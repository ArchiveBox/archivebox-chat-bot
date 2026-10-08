"""Native macOS Messages through maintained imsg JSON-RPC, locally or over SSH.

Only public read/send surfaces are used. Native GUID-targeted replies/tapbacks
require imsg's private bridge and are deliberately unavailable here.
"""

import asyncio
import contextlib
import hashlib
import json
import re
import shlex
from pathlib import Path


class IMessageTransport:
    def __init__(self, options: dict, account_id: str, data_dir: Path, emit):
        self.options, self.account_id, self.emit = options, account_id, emit
        self.process = None
        self.pending = {}
        self.sequence = 0
        self.reader = self.consumer = None
        self.inbox = asyncio.Queue(maxsize=1024)
        self.error = ""
        scope = json.dumps([account_id, options.get("ssh_host"), options.get("ssh_user"), options.get("db_path")])
        self.cursor_path = Path(data_dir) / f"imessage-{hashlib.sha256(scope.encode()).hexdigest()[:24]}.json"
        self.cursor = json.loads(self.cursor_path.read_text()) if self.cursor_path.exists() else {}
        self.own_handles = {
            handle.casefold()
            for handle in [*options.get("own_handles", []), *self.cursor.get("own_handles", [])]
            if handle
        }

    def _command(self):
        args = [self.options.get("imsg_path", "imsg"), "rpc"]
        if self.options.get("db_path"):
            args += ["--db", self.options["db_path"]]
        host = self.options.get("ssh_host")
        if not host:
            return args
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
            raise ValueError("Invalid SSH host")
        ssh = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(int(self.options.get("ssh_port", 22))),
        ]
        if self.options.get("ssh_user"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", self.options["ssh_user"]):
                raise ValueError("Invalid SSH user")
            ssh += ["-l", self.options["ssh_user"]]
        if self.options.get("ssh_identity_file"):
            ssh += ["-i", self.options["ssh_identity_file"], "-o", "IdentitiesOnly=yes"]
        if self.options.get("ssh_known_hosts_file"):
            ssh += ["-o", "UserKnownHostsFile=" + self.options["ssh_known_hosts_file"]]
        # SSH runs its remote command through a shell; quote every remote argument.
        return ssh + [host, shlex.join(args)]

    async def _open(self):
        if self.process:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self._command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024 * 1024,
            )
        except OSError:
            raise RuntimeError(
                "Cannot start imsg; install it on the Mac and verify executable/SSH configuration"
            ) from None
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                item = json.loads(line)
                if "id" in item:
                    future = self.pending.get(item["id"])
                    if future and not future.done():
                        future.set_result(item)
                elif item.get("method") == "message":
                    self.inbox.put_nowait(item["params"]["message"])
                elif item.get("method") in {"watch.overflow", "error", "watch.error"}:
                    raise RuntimeError("imsg watch ended; reconnect to resume from the persisted cursor")
            raise RuntimeError("imsg disconnected; verify Mac access, SSH host key, and process permissions")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - transport boundary must redact credentials and surface failure
            self.error = "imsg connection/watch failed; verify Mac access, SSH host key and permissions, then reconnect"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.error))

    async def _rpc(self, method, params=None):
        await self._open()
        if self.error:
            raise RuntimeError(self.error)
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n"
                ).encode()
            )
            await self.process.stdin.drain()
            response = await asyncio.wait_for(future, 60)
        except TimeoutError:
            raise RuntimeError(
                "imsg response timed out; sending may have completed. Inspect Messages before retrying"
            ) from None
        finally:
            self.pending.pop(request_id, None)
        if "error" in response:
            code = response["error"].get("code")
            detail = response["error"].get("data", {})
            if isinstance(detail, dict) and "-1743" in str(detail.get("detail", "")):
                raise RuntimeError(
                    "Messages Automation permission denied: allow the supervising app to control Messages on the Mac"
                )
            if code == -32002:
                raise RuntimeError(
                    "Messages database unavailable: grant Full Disk Access to the supervising process on the Mac"
                )
            if code in {-32001, -32004}:
                raise RuntimeError("iMessage delivery is uncertain/in flight; inspect Messages before retrying")
            raise RuntimeError(f"imsg {method} failed (code {code}); check Messages Automation permission and target")
        return response["result"]

    async def check(self):
        status = await self._rpc("initialize", {"protocol_version": 1})
        if not status.get("database", {}).get("ready"):
            raise RuntimeError(
                "Messages database unavailable: grant Full Disk Access to the supervising process on the Mac"
            )
        return {
            "id": self.account_id,
            "name": "Messages on " + self.options.get("ssh_host", "this Mac"),
            "version": status.get("version"),
            "capabilities": {"reactions": False, "media": False, "threads": False},
        }

    async def start(self):
        await self.check()
        if self.cursor:
            # ROWIDs are database-local. Verify the checkpoint's GUID before reuse.
            page = await self._rpc("messages.after", {"since_rowid": self.cursor["rowid"] - 1, "limit": 1})
            rows = page.get("messages", [])
            if not rows or rows[0].get("guid") != self.cursor["guid"]:
                raise RuntimeError(
                    "Messages database checkpoint changed; verify the Mac/database and reset the cursor explicitly"
                )
        self.consumer = asyncio.create_task(self._consume())
        params = {"attachments": False, "include_reactions": False}
        if self.cursor:
            params["since_rowid"] = self.cursor["rowid"]
        await self._rpc("watch.subscribe", params)

    async def _consume(self):
        try:
            while True:
                message = await self.inbox.get()
                rowid, guid = message.get("id"), message.get("guid")
                if not rowid or not guid or not message.get("chat_id"):
                    raise RuntimeError("imsg emitted a message without stable IDs")
                sender = message.get("sender", "").casefold()
                local_handle = (message.get("destination_caller_id") or "").casefold()
                if message.get("is_from_me") and local_handle:
                    # Outgoing `sender` is a raw database handle and may identify
                    # the recipient. Only the documented local routing identity
                    # is safe to learn as our own address.
                    self.own_handles.add(local_handle)
                # Self chats produce a second incoming row with a DIFFERENT GUID.
                # There is no public API link to distinguish that echo from local
                # human input; exclude known local handles instead of matching text.
                if not message.get("is_from_me") and sender not in self.own_handles and not message.get("is_reaction"):
                    text = message.get("text") or ""
                    mention = self.options.get("mention", "@archivebox")
                    await self.emit(
                        {
                            "id": guid,
                            "channel": str(message["chat_id"]),
                            "user": message.get("sender", ""),
                            "user_name": message.get("sender_name") or message.get("sender", ""),
                            "text": text,
                            "thread": message.get("thread_originator_guid") or message.get("reply_to_guid") or "",
                            "is_dm": not message.get("is_group", True),
                            "is_mention": bool(
                                mention and re.search(re.escape(mention) + r"(?!\w)", text, re.IGNORECASE)
                            ),
                            "is_bot": False,
                        }
                    )
                # Advance only after core has durably accepted the event.
                self.cursor = {"rowid": int(rowid), "guid": guid, "own_handles": sorted(self.own_handles)}
                self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cursor_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.cursor))
                temporary.chmod(0o600)
                temporary.replace(self.cursor_path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - transport boundary must redact credentials and surface failure
            self.error = (
                "iMessage ingestion failed; reconnect after checking durable inbox/storage. Cursor was not advanced"
            )

    async def send(self, channel: str, text: str, thread: str = "", media: list[dict] | None = None):
        if media:
            raise ValueError("iMessage attachment uploads are not supported by this transport")
        if thread:
            raise ValueError("iMessage native thread replies require the private bridge and are unsupported")
        if not channel.isdecimal() or int(channel) < 1:
            raise ValueError("iMessage destination must be a positive chat ID from the channel list")
        response = await self._rpc(
            "send", {"chat_id": int(channel), "text": text, "service": "imessage", "transport": "applescript"}
        )
        if not response.get("ok") or not response.get("guid"):
            raise RuntimeError("iMessage send has no verified message GUID; inspect Messages before retrying")
        return response["guid"]

    async def react(self, channel: str, message_id: str, status: str):
        raise NotImplementedError(
            "Normal imsg tapbacks cannot target a message reliably; GUID targeting requires the private bridge"
        )

    async def join(self, channel: str):
        for chat in await self.channels():
            if chat["id"] == channel:
                return chat
        raise ValueError("iMessage cannot create/join chats here; choose an existing Messages conversation")

    async def channels(self):
        response = await self._rpc("chats.list", {"limit": 1000})
        return [
            {
                "id": str(chat["id"]),
                "name": chat.get("name") or chat.get("identifier") or str(chat["id"]),
                "is_dm": not chat.get("is_group", True),
            }
            for chat in response.get("chats", [])
        ]

    async def close(self):
        for task in (self.consumer, self.reader):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.terminate()
                await self.process.wait()
        self.process = None
