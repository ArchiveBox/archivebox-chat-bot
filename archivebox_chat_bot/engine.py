import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import httpx
from slack_sdk.errors import SlackApiError

from .archivebox import ArchiveBox, OpenCodeError
from .models import Message
from .store import now
from .text import extract_urls, slack_escape, submission_tags, submitter_tag

log = logging.getLogger(__name__)
HELP_TEXT = "📚 /archivebox save <URLs> · search <words> · status · auto on/off · help"


def safe_error(error):
    if isinstance(error, SlackApiError):
        code = error.response.get("error", "request failed")
        return {
            "invalid_auth": "Slack could not verify this token. Copy it again from your Slack app's settings.",
            "token_revoked": "Slack access was revoked. Reinstall your Slack app and copy its new token.",
            "missing_scope": "Your Slack app needs another permission. Use the preconfigured app setup and reinstall it.",
        }.get(code, f"Slack: {code}")
    if isinstance(error, httpx.TransportError):
        return "Connection interrupted. Inspect the remote result before retrying."
    if isinstance(error, TimeoutError):
        return str(error) or "Connection timed out. Check the server URL and network."
    return str(error)[:500]


class Engine:
    def __init__(self, store, settings, connection, adapters):
        self.store = store
        self.settings = settings
        self.connection = connection
        self.adapters = adapters
        self.archive = None
        self.bots = {}
        self.tasks = set()
        self.active_jobs = {}
        self.loop = None
        self.error = ""
        self.connections = {}

    def scope(self, role="capture"):
        identity = getattr(self.bots.get(role), "identity", {})
        options = self.connection.account_options(role)
        # Preserve the existing single-connection identity during settings migration.
        identity_parts = [
            self.connection.platform,
            self.settings.archivebox_url,
            self.connection.saved_channel if role == "capture" else "",
            role,
            identity.get("team_id", ""),
            identity.get("user_id", ""),
            options.get("url", "") if self.connection.platform == "zulip" else "",
            options.get("email", "") if self.connection.platform == "zulip" else "",
        ]
        if self.connection.id != "default":
            identity_parts.append(self.connection.id)
        return hashlib.sha256(json.dumps(identity_parts).encode()).hexdigest()[:24]

    def bind_source(self, role):
        bot = self.bots.get(role)
        identity = getattr(bot, "identity", {})
        user = identity.get("user_id") or getattr(bot, "bot_id", "")
        if not user:
            return False
        options = self.connection.account_options(role)
        source = {
            key: options.get(key)
            for key in (
                "url",
                "server",
                "port",
                "tls",
                "homeserver",
                "transport",
                "ssh_host",
                "ssh_user",
                "db_path",
                "own_handles",
                "base_url",
                "account_id",
            )
        }
        fingerprint = hashlib.sha256(
            json.dumps([self.connection.platform, identity.get("team_id"), user, source], sort_keys=True).encode()
        ).hexdigest()
        self.store.bind_identity(self.connection.id, role, fingerprint)
        if role == "capture" and not self.store.meta("sealed_cursor:" + self.scope()):
            self.store.set_meta("sealed_cursor:" + self.scope(), now())
        return True

    async def start(self):
        self.error = ""
        self.archive = ArchiveBox(self.settings)
        for role in ("capture", "ai"):
            if not getattr(self.connection, role).enabled:
                continue
            started = time.perf_counter()
            self.store.event(
                "connection", f"Connecting {self.connection.platform}", connection=self.connection.id, role=role
            )
            try:
                bot = self.adapters.create(self.connection, role, self.settings)
                self.bots[role] = bot
                if (
                    self.connection.platform in {"slack", "discord"}
                    and role == "capture"
                    and (
                        (self.connection.enable_new_urls and not self.connection.new_channel)
                        or (self.connection.enable_saved_urls and not self.connection.saved_channel)
                    )
                ):
                    # Resolve destinations before receiving messages or binding durable job scopes.
                    await bot.check()
                    for field, channel in (await bot.setup_channels()).items():
                        setattr(self.connection, field, channel)
                    self.store.save_settings(self.settings)
                await bot.start(self.receive)
                self.bind_source(role)
                self.connections[role] = {"ok": True, "capabilities": getattr(bot, "capabilities", {})}
                self.store.event(
                    "connection",
                    "Connected",
                    connection=self.connection.id,
                    role=role,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            except Exception as exc:
                log.exception("%s:%s connection failed", self.connection.id, role)
                self.connections[role] = {"ok": False, "error": safe_error(exc)}
                self.store.event("connection", safe_error(exc), level="error", connection=self.connection.id, role=role)
        self.loop = asyncio.create_task(self.run())

    async def close(self):
        if self.loop:
            self.loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.active_jobs.clear()
        for bot in self.bots.values():
            await bot.close()
        await self.archive.close()
        self.bots.clear()
        self.connections.clear()
        self.store.event("connection", "Disconnected", connection=self.connection.id)

    def permitted(self, message):
        s = self.connection
        if message.is_bot or message.platform != s.platform:
            return
        if s.allowed_users and message.user not in s.allowed_users:
            return
        if (
            not message.is_dm
            and message.command != "__stop"
            and s.allowed_channels
            and message.channel not in s.allowed_channels
        ):
            return
        if message.role == "ai":
            if not s.ai.enabled or message.user not in s.ai_allowed_users:
                return
            enabled = (
                message.command == "__stop"
                or (message.is_dm and s.enable_dms)
                or (message.is_mention and s.enable_mentions)
            )
        elif message.command:
            enabled = message.command in s.commands
        else:
            enabled = (
                (message.is_dm and s.enable_dms)
                or (message.is_mention and s.enable_mentions)
                or (message.channel == s.new_channel and s.enable_new_urls)
                or (
                    not message.is_dm
                    and message.channel != s.saved_channel
                    and self.store.group_policy(s.id, message.channel, s.auto_archive_groups)
                )
            )
        return enabled

    async def receive(self, message):
        if not self.bind_source(message.role):
            raise RuntimeError("The connected bot identity is not available yet")
        message.connection = self.connection.id
        self.store.remember(self.connection.id, message)
        permitted = self.permitted(message)
        if message.command and not message.is_bot:
            self.store.event(
                "command",
                f"/{message.command} · {'accepted' if permitted else 'ignored by permissions'} · channel {message.channel} · user {message.user}",
                connection=self.connection.id,
                role=message.role,
            )
        if permitted:
            inserted = self.store.enqueue(
                self.scope(message.role) + ":" + message.key,
                "message",
                message.as_dict(),
                scope=self.scope(message.role),
            )
            if inserted:
                self.store.event(
                    "message",
                    f"Queued {message.role} message · channel {message.channel} · user {message.user}",
                    connection=self.connection.id,
                    role=message.role,
                )
            return True
        return False

    async def run(self):
        last_poll = 0
        while True:
            try:
                queued = self.store.jobs("queued", limit=100, scope=self.scope()) + self.store.jobs(
                    "queued", limit=100, scope=self.scope("ai")
                )
                for job in sorted(queued, key=lambda row: row["payload"].get("command") != "__stop"):
                    if not self.bind_source(job["payload"].get("role", "capture")):
                        continue
                    if job["payload"].get("command") == "__stop":
                        await self.stop_agent(job)
                        continue
                    if len(self.tasks) >= 4:
                        continue
                    if not self.settings.archivebox_token or not self.bots.get(job["payload"].get("role", "capture")):
                        break
                    self.store.update(job["id"], "running")
                    task = asyncio.create_task(self.process(job))
                    self.tasks.add(task)
                    self.active_jobs[job["id"]] = task
                    task.add_done_callback(lambda task, key=job["id"]: self.active_jobs.pop(key, None))
                    task.add_done_callback(self.tasks.discard)
                if asyncio.get_running_loop().time() - last_poll >= self.settings.poll_seconds:
                    last_poll = asyncio.get_running_loop().time()
                    await self.monitor()
                    await self.monitor_agents()
                    await self.discover()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Bridge polling failed")
                self.error = safe_error(exc)
            await asyncio.sleep(1)

    async def stop_agent(self, stop_job):
        message = Message(**stop_job["payload"])
        bot = self.bots.get("ai")
        if not bot or message.user not in self.connection.ai_allowed_users:
            self.store.update(stop_job["id"], "ignored")
            return
        for job in self.store.jobs("running", scope=self.scope("ai")) + self.store.jobs(
            "agent_waiting", scope=self.scope("ai")
        ):
            payload = job["payload"]
            if (
                payload.get("role") == "ai"
                and payload.get("channel") == message.channel
                and (payload.get("thread") or payload.get("ts")) == message.thread
            ):
                session = job["result"].get("session")
                if session:
                    await self.archive.abort_session(session)
                task = self.active_jobs.get(job["id"])
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self.store.update(job["id"], "cancelled")
                await self.reaction(bot, Message(**payload), "x")
        await bot.agent_status(message, "closed")
        self.store.update(stop_job["id"], "done")

    async def reaction(self, bot, message, status):
        if not getattr(bot, "capabilities", {}).get("reactions", True):
            return
        try:
            await bot.react(message, status)
        except Exception as exc:
            log.exception("Reaction failed")
            self.error = f"Reaction: {safe_error(exc)}"

    async def process(self, job):
        message = None
        bot = None
        committed = False
        try:
            if job["kind"] == "announcement":
                bot = self.bots["capture"]
                if not self.connection.enable_saved_urls or not self.connection.saved_channel:
                    self.store.update(job["id"], "queued")
                    return
                snapshot = job["payload"]
                media = {}
                if self.connection.upload_images:
                    for kind in ("screenshot", "favicon"):
                        artifact = await self.archive.artifact(snapshot, kind)
                        if artifact:
                            media[kind] = artifact
                # Marked running before crossing the network; ambiguous deliveries are never auto-replayed.
                result = await bot.post_card(
                    snapshot, self.archive.detail_url(snapshot), media, str(uuid.uuid5(uuid.NAMESPACE_URL, job["id"]))
                )
                self.store.update(job["id"], "done", {"message_id": result})
                return
            message = Message(**job["payload"])
            if not self.permitted(message):
                self.store.update(job["id"], "ignored", error="Feature or permission changed before processing")
                return
            bot = self.bots.get(message.role)
            if not bot:
                raise ValueError("This bot is disabled; enable it before retrying")
            user = await bot.user(message.user)
            if user["is_bot"] or (user["is_guest"] and not self.connection.allow_guests):
                self.store.update(job["id"], "ignored")
                return
            name = submitter_tag(user["name"], message.user)
            await self.reaction(bot, message, "runner")
            if message.command and message.command != "save":
                await self.command(bot, message)
                await self.reaction(bot, message, "white_check_mark")
                self.store.update(job["id"], "done")
                return
            if not message.is_dm and not message.channel_name:
                channel = next((row for row in await bot.channels() if row["id"] == message.channel), {})
                message.channel_name = channel.get("name", "") if channel.get("name") != message.channel else ""
                message.is_dm = channel.get("is_dm", False)
            tags = submission_tags(
                message.source_platform or message.platform, name, "" if message.is_dm else message.channel_name
            )
            context = []
            if message.is_mention or (message.role == "ai" and message.is_dm):
                context = await bot.recent(message)
            text = "\n".join([*context, message.text])
            if message.role == "ai":
                title = message.text
                if message.platform == "slack":
                    await bot.agent_status(message, "processing")
                    title = title.replace(f"<@{bot.identity['user_id']}>", "")
                title = " ".join(title.split())[:70]
                session = await self.archive.create_session(f"{message.platform.title()} · {name} · {title}")
                self.store.update(
                    job["id"], "running", {"session": session, "session_url": self.archive.session_url(session)}
                )
                prompt = (
                    f"{self.settings.ai_prompt} Keep replies on one compact line whenever possible; avoid lists unless asked.\n"
                    f"ArchiveBox public URL: {self.settings.archivebox_public_url}\n"
                    f"Collection directory: {session['directory']}\n\n"
                    f"Apply these separate source tags to every URL you add, alongside any requested tags: {json.dumps(tags)}.\n"
                    f"Request from {name} ({message.user}) on {message.platform}. Earlier conversation is context; "
                    f"the final message is the current request.\n<conversation>\n{text}\n</conversation>"
                )
                result = {"session": session, "session_url": self.archive.session_url(session), "phase": "agent_result"}
                self.store.update(job["id"], "running", result)
                await self.archive.submit_session(session, prompt)
                self.store.update(job["id"], "agent_waiting", result)
                return
            urls = extract_urls(text)
            self.store.event(
                "parse",
                f"Parsed {len(urls)} HTTP(S) URL(s) · channel {message.channel}",
                connection=self.connection.id,
                role=message.role,
            )
            if not urls:
                self.store.update(job["id"], "ignored", error="No HTTP(S) URLs found")
                await self.reaction(bot, message, "x")
                if message.command:
                    await bot.post_text(message, "No HTTP(S) URLs found. Include the links you want to save.")
                return
            if len(urls) > self.settings.max_urls:
                raise ValueError(f"Message contains {len(urls)} URLs; configured limit is {self.settings.max_urls}")
            result = await self.archive.add(urls, tags)
            committed = True
            result["urls"] = urls
            result["tags"] = tags
            self.store.update(job["id"], "waiting", result)
            self.store.event(
                "capture", f"Queued {len(urls)} URL(s) in ArchiveBox", connection=self.connection.id, role=message.role
            )
            if message.command:
                await bot.post_text(
                    message, f"Queued {len(urls)} URL(s). Completed captures appear in the Saved URLs channel."
                )
        except asyncio.CancelledError:
            if not committed:
                self.store.update(
                    job["id"],
                    "uncertain",
                    error="Configuration changed during a remote operation. Inspect before retrying.",
                )
            raise
        except Exception as exc:
            log.exception("Job failed")
            # Any failed mutation may have reached the remote server. Require human reconciliation.
            if not committed:
                self.store.update(job["id"], "uncertain", error=safe_error(exc))
            else:
                self.error = safe_error(exc)
            if message and bot:
                await self.reaction(bot, message, "x")
                if message.native_command:
                    with contextlib.suppress(Exception):
                        await bot.post_text(message, f"❌ {safe_error(exc)} · Check Activity before retrying.")
                if message.role == "ai" and message.platform == "slack":
                    with contextlib.suppress(SlackApiError):
                        await bot.agent_status(message, "suspended")

    async def command(self, bot, message):
        if message.command == "help":
            text = HELP_TEXT
        elif message.command == "status":
            status = await self.archive.check()
            text = f"✅ ArchiveBox connected · {status['snapshots']} snapshots · persona {self.settings.persona}"
        elif message.command == "auto":
            option = message.text.strip().lower()
            if message.is_dm:
                text = "Use /archivebox auto on or off inside the group you want to configure."
            elif message.user not in self.connection.admin_users:
                text = "Only configured bot administrators can change group archiving."
            elif option not in {"on", "off", "status", ""}:
                text = "Use /archivebox auto on, off, or status."
            else:
                if option in {"on", "off"}:
                    self.store.set_group(self.connection.id, message.channel, auto_archive=option == "on")
                enabled = self.store.group_policy(
                    self.connection.id, message.channel, self.connection.auto_archive_groups
                )
                text = "🔗 Archive every link in this group: " + ("on" if enabled else "off")
        elif message.command == "search":
            if not message.text.strip():
                text = "Use /archivebox search <words>"
            else:
                results = await self.archive.search(message.text)
                if message.platform == "slack":
                    links = [
                        f"<{self.archive.detail_url(s)}|{slack_escape(s.get('title') or s['url'])}>" for s in results
                    ]
                elif message.platform == "zulip":
                    from .zulip import _link, _markdown

                    links = [
                        f"[{_markdown(s.get('title') or s['url'])}]({_link(self.archive.detail_url(s))})"
                        for s in results
                    ]
                else:
                    links = [f"{s.get('title') or s['url']} — {self.archive.detail_url(s)}" for s in results]
                text = " · ".join(links) or "No saved pages found."

        else:
            raise ValueError("Unknown command")
        await bot.post_text(message, text)

    async def monitor_agents(self):
        jobs = self.store.jobs("agent_waiting", scope=self.scope("ai"))
        # A lost submit acknowledgment can be reconciled by reading the existing
        # session. Never submit the prompt again or replay an ambiguous delivery.
        jobs += [
            job
            for job in self.store.jobs("uncertain", scope=self.scope("ai"))
            if job["result"].get("phase") == "agent_result"
        ]
        for job in jobs:
            message = Message(**job["payload"])
            bot = self.bots.get("ai")
            if not bot or not self.permitted(message):
                continue
            result = job["result"]
            try:
                answer = await self.archive.session_answer(result["session"])
                if answer is None:
                    continue
                result = {**result, "answer": answer, "phase": "agent_delivery"}
                self.store.update(job["id"], "running", result)
                sent = await bot.post_text(message, answer)
                self.store.update(job["id"], "done", {**result, "message_id": sent})
                await self.reaction(bot, message, "white_check_mark")
                if message.platform == "slack":
                    await bot.agent_status(message, "active")
            except asyncio.CancelledError:
                if result.get("phase") == "agent_delivery":
                    self.store.update(
                        job["id"],
                        "uncertain",
                        result,
                        error="Answer delivery interrupted; inspect chat before retrying.",
                    )
                raise
            except OpenCodeError as exc:
                # OpenCode already recorded a final error. Polling this same
                # response again only floods the activity log; retry is explicit.
                self.store.update(job["id"], "failed", result, error=safe_error(exc))
                self.error = safe_error(exc)
            except Exception as exc:
                log.exception("Agent result reconciliation failed")
                if self.store.get(job["id"])["state"] == "done":
                    self.error = safe_error(exc)
                    continue
                self.store.update(job["id"], "uncertain", result, error=safe_error(exc))
                self.error = safe_error(exc)

    async def monitor(self):
        for job in self.store.jobs("waiting", limit=100, scope=self.scope()):
            if job["payload"]["platform"] != self.connection.platform:
                continue
            crawl = await self.archive.crawl(job["result"]["crawl_id"])
            if crawl["status"] != "sealed":
                continue
            message = Message(**job["payload"])
            bot = self.bots.get(message.role)
            if not bot:
                continue
            # Every accepted human submission creates a fresh crawl with
            # ONLY_NEW=False. A different crawl's capture cannot finish this job.
            crawl_id = str(crawl["id"]).replace("-", "").lower()
            snapshots = await self.archive.crawl_snapshots(crawl["id"])
            requested = job["result"].get("urls", [])
            owned = {}
            for snapshot in snapshots:
                if str(snapshot.get("crawl_id", "")).replace("-", "").lower() == crawl_id:
                    owned.setdefault(snapshot["url"], []).append(snapshot)
            failures = []
            pending = False
            if not requested:
                failures.append("The submission has no recorded URLs")
            for url in requested:
                candidates = owned.get(url, [])
                if not candidates:
                    failures.append(f"No snapshot was created in this crawl for {url}")
                    continue
                if any(snapshot["status"] in {"queued", "started"} for snapshot in candidates):
                    pending = True
                    continue
                if any(snapshot["status"] != "sealed" for snapshot in candidates):
                    failures.append(f"Capture was paused or cancelled before sealing for {url}")
                    continue
                # Browser lifecycle markers alone are not successful captures.
                # At least one real saved output must have a nonempty manifest.
                successful = any(
                    result.get("status") == "succeeded"
                    and result.get("plugin") not in {"chrome", "base", "opencode"}
                    and any(
                        isinstance(metadata, dict)
                        and isinstance(metadata.get("size"), (int, float))
                        and metadata["size"] > 0
                        for metadata in (result.get("output_files") or {}).values()
                    )
                    for snapshot in candidates
                    for result in snapshot.get("archiveresults", [])
                )
                if not successful:
                    failures.append(f"Capture sealed without successful saved outputs for {url}")
            # A reused or concurrently queued URL in another crawl is never
            # used as evidence. Keep this job running while its own rows settle.
            if pending:
                continue
            good = not failures
            await self.reaction(bot, message, "white_check_mark" if good else "x")
            result = {**job["result"], "snapshot_ids": [snapshot["id"] for snapshot in snapshots]}
            self.store.update(
                job["id"], "done" if good else "failed", result, error="" if good else "\n".join(failures)
            )

    async def discover(self):
        if (
            not self.connection.enable_saved_urls
            or not self.connection.saved_channel
            or not self.settings.archivebox_token
            or not self.bind_source("capture")
        ):
            return
        cursor = self.store.meta("sealed_cursor:" + self.scope())
        # Fixed upper bound plus overlap handles captures finishing during pagination.
        lower = (datetime.fromisoformat(cursor) - timedelta(minutes=2)).isoformat()
        upper = now()
        offset = 0
        while True:
            page = await self.archive.snapshots(
                status="sealed",
                modified_at__gte=lower,
                modified_at__lt=upper,
                limit=100,
                offset=offset,
                with_archiveresults="true",
            )
            for snapshot in page["items"]:
                self.store.enqueue(
                    f"saved:{self.scope()}:{snapshot['id']}",
                    "announcement",
                    {**snapshot, "connection": self.connection.id},
                    scope=self.scope(),
                )
            offset += len(page["items"])
            if not page["items"] or offset >= page["total_items"]:
                break
        self.store.set_meta("sealed_cursor:" + self.scope(), upper)
        self.error = ""
