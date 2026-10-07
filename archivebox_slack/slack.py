import asyncio
import logging
from collections import deque
from urllib.parse import urlsplit

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from .media import normalize_image
from .models import Message
from .text import size_label, slack_escape

log = logging.getLogger(__name__)


class Slack:
    def __init__(self, settings, role="capture"):
        self.settings, self.role = settings, role
        token = settings.slack_ai_bot_token if role == "ai" else settings.slack_bot_token
        self.client = AsyncWebClient(
            token=token, timeout=30, retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=3)]
        )
        self.history = AsyncWebClient(
            token=settings.slack_history_token or token,
            timeout=30,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=3)],
        )
        self.socket = None
        self.identity = {}
        self.on_message = None
        self.last_error = ""

    async def check(self):
        self.identity = (await self.client.auth_test()).data
        return {
            "name": self.identity["user"],
            "team": self.identity["team"],
            "user_id": self.identity["user_id"],
            "team_id": self.identity["team_id"],
        }

    async def start(self, on_message):
        await self.check()
        self.on_message = on_message
        app_token = self.settings.slack_ai_app_token if self.role == "ai" else self.settings.slack_app_token
        if self.settings.slack_transport == "socket":
            self.socket = SocketModeClient(app_token=app_token, web_client=self.client)
            self.socket.socket_mode_request_listeners.append(self._envelope)
            await self.socket.connect()

    async def _envelope(self, client, request):
        # Persist before ack, but never wait for archiving or Slack Web API work.
        if request.type == "events_api":
            await self.receive(request.payload)
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        elif request.type == "slash_commands":
            text = request.payload.get("text", "")
            command = text.split(maxsplit=1)[0] if text.strip() else "help"
            if command not in self.settings.commands:
                payload = {
                    "text": "That command is disabled. Ask your ArchiveBox administrator.",
                    "response_type": "ephemeral",
                }
            else:
                await self.receive_command(request.payload)
                payload = {
                    "text": "Working on it — results will appear in this conversation.",
                    "response_type": "ephemeral",
                }
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id, payload=payload))

    async def receive(self, payload):
        if self.identity and payload.get("team_id") != self.identity.get("team_id"):
            return
        event = payload.get("event", {})
        if event.get("type") == "agent_session_stopped" and self.role == "ai":
            await self.on_message(
                Message(
                    "slack",
                    "ai",
                    event["channel"],
                    event["user"],
                    "",
                    event["event_ts"],
                    thread=event["thread_ts"],
                    command="__stop",
                )
            )
            return
        if event.get("type") == "app_home_opened":
            # UI publish is independent of the event acknowledgment.
            asyncio.create_task(self.publish_home(event["user"]))
            return
        if event.get("type") not in ("message", "app_mention"):
            return
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        if not event.get("user") or event["user"] == self.identity.get("user_id"):
            return
        text = event.get("text", "")
        own = self.identity.get("user_id", "")
        message = Message(
            platform="slack",
            role=self.role,
            channel=event["channel"],
            user=event["user"],
            text=text,
            ts=event["ts"],
            thread=event.get("thread_ts", ""),
            is_dm=event.get("channel_type") == "im",
            is_mention=event["type"] == "app_mention" or bool(own and f"<@{own}>" in text),
        )
        if self.on_message:
            await self.on_message(message)

    async def receive_command(self, payload):
        if self.identity and payload.get("team_id") != self.identity.get("team_id"):
            return
        raw = payload.get("text", "").strip()
        command, _, text = raw.partition(" ")
        message = Message(
            "slack",
            self.role,
            payload["channel_id"],
            payload["user_id"],
            text,
            payload.get("trigger_id", ""),
            command=command or "help",
        )
        await self.on_message(message)

    async def publish_home(self, user_id):
        try:
            await self.client.views_publish(
                user_id=user_id,
                view={
                    "type": "home",
                    "blocks": [
                        {"type": "header", "text": {"type": "plain_text", "text": "Your team's web archive 📚"}},
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"• Drop links in <#{self.settings.new_channel}>\n• Mention *@archivebox* in a thread to save its latest links\n"
                                    f"• DM links directly to me\n• Browse completed captures in <#{self.settings.saved_channel}>\n"
                                    "• `/archivebox search words` finds saved pages"
                                ),
                            },
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Open ArchiveBox"},
                                    "url": self.settings.archivebox_public_url,
                                }
                            ],
                        },
                    ],
                },
            )
        except SlackApiError as exc:
            self.last_error = exc.response.get("error", "Home tab failed")

    async def setup_channels(self):
        result = {}
        for field in ("new_channel", "saved_channel"):
            if not getattr(self.settings, "enable_new_urls" if field == "new_channel" else "enable_saved_urls"):
                result[field] = getattr(self.settings, field)
                continue
            existing = getattr(self.settings, field)
            if existing:
                info = (await self.client.conversations_info(channel=existing))["channel"]
                if not info.get("is_member"):
                    if info.get("is_private"):
                        raise ValueError("Invite the bot to this private channel first")
                    await self.client.conversations_join(channel=existing)
                result[field] = existing
                continue
            name = getattr(self.settings, f"{field}_name")
            try:
                response = await self.client.conversations_create(name=name)
                channel = response["channel"]["id"]
            except SlackApiError as exc:
                if exc.response.get("error") != "name_taken":
                    raise
                cursor = None
                channel = None
                while True:
                    response = await self.client.conversations_list(
                        limit=200, cursor=cursor, exclude_archived=True, types="public_channel,private_channel"
                    )
                    channel = next((item["id"] for item in response["channels"] if item["name"] == name), None)
                    cursor = response.get("response_metadata", {}).get("next_cursor")
                    if channel or not cursor:
                        break
                if not channel:
                    raise ValueError(
                        f"Channel {name} exists but is not accessible; invite the bot and enter its channel ID"
                    ) from exc
            await self.client.conversations_join(channel=channel)
            result[field] = channel
        return result

    async def user(self, user_id):
        data = (await self.client.users_info(user=user_id))["user"]
        profile = data.get("profile", {})
        return {
            "name": profile.get("display_name") or profile.get("real_name") or data["name"],
            "is_bot": data.get("is_bot", False),
            "is_guest": data.get("is_restricted", False) or data.get("is_ultra_restricted", False),
        }

    async def recent(self, message):
        if not message.thread:
            return []
        tail = deque(maxlen=10)
        cursor = None
        reader = self.client if message.is_dm else self.history
        while True:
            result = await reader.conversations_replies(
                channel=message.channel, ts=message.thread, latest=message.ts, inclusive=True, limit=15, cursor=cursor
            )
            for item in result["messages"]:
                if item.get("ts") != message.ts and not item.get("bot_id") and item.get("user"):
                    tail.append(item.get("text", ""))
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return list(tail)

    async def react(self, message, status):
        if message.command:
            return
        try:
            await self.client.reactions_add(channel=message.channel, timestamp=message.ts, name=status)
        except SlackApiError as exc:
            if exc.response.get("error") != "already_reacted":
                raise
        if status != "runner":
            try:
                await self.client.reactions_remove(channel=message.channel, timestamp=message.ts, name="runner")
            except SlackApiError as exc:
                if exc.response.get("error") != "no_reaction":
                    raise

    async def agent_status(self, message, status):
        details = {
            "channel_id": message.channel,
            "thread_ts": message.thread or message.ts,
            "status": status,
            "initiator_user_id": message.user,
        }
        if message.text:
            details["title"] = " ".join(message.text.replace(f"<@{self.identity['user_id']}>", "").split())[:160]
        await self.client.api_call(
            "agents.sessions.setStatus",
            json=details,
        )

    async def post_text(self, message, text):
        # Commands are private to their caller. AI answers are visible in the originating thread.
        if message.command:
            result = await self.client.chat_postEphemeral(channel=message.channel, user=message.user, text=text[:39000])
            return result.get("message_ts", "")
        result = await self.client.chat_postMessage(
            channel=message.channel,
            thread_ts=message.thread or message.ts,
            text=text[:39000],
            unfurl_links=False,
            unfurl_media=False,
        )
        return result["ts"]

    async def post_card(self, snapshot, detail_url, media, delivery_id):
        files = {}
        for kind, (data, _mime) in media.items():
            response = await self.client.files_upload_v2(
                file=normalize_image(data, kind),
                filename=f"{kind}.png",
                title=f"{snapshot.get('title') or snapshot['url']} · {kind}",
            )
            file_id = response["files"][0]["id"]
            # completeUploadExternal acknowledges storage before Slack's image processor finishes.
            # Poll its actual file metadata; posting an unprocessed file produces invalid_blocks.
            deadline = asyncio.get_running_loop().time() + 60
            while True:
                file = (await self.client.files_info(file=file_id))["file"]
                if file.get("filetype") == "png" and file.get("original_w"):
                    break
                if file.get("filetype") and file["filetype"] != "png":
                    raise ValueError("Slack did not recognize the uploaded PNG image")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Slack is still processing the uploaded image; inspect this job before retrying")
                await asyncio.sleep(1)
            files[kind] = file
        title = slack_escape(" ".join((snapshot.get("title") or snapshot["url"]).split()))[:100]
        original = slack_escape(snapshot["url"])
        source = slack_escape(urlsplit(snapshot["url"]).hostname or snapshot["url"])
        size = size_label(snapshot.get("output_size", 0))
        persona = slack_escape(" ".join((snapshot.get("persona") or "Default").split()))
        line = f"✅ *<{detail_url}|{title}>* · <{original}|{source}> · {size} · 👤 {persona}"
        if files.get("favicon"):
            line += f" · <{files['favicon']['permalink']}|🌐>"
        section = {"type": "section", "text": {"type": "mrkdwn", "text": line}}
        if files.get("screenshot"):
            section["accessory"] = {"type": "image", "slack_file": {"id": files["screenshot"]["id"]}, "alt_text": "📷"}
        result = await self.client.chat_postMessage(
            channel=self.settings.saved_channel,
            client_msg_id=delivery_id,
            text=line,
            blocks=[section],
            unfurl_links=False,
            unfurl_media=False,
        )
        return result["ts"]

    async def close(self):
        if self.socket:
            await self.socket.close()
        for client in (self.client, self.history):
            if client.session and not client.session.closed:
                await client.session.close()
