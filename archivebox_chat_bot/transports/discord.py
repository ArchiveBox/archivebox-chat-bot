"""Discord Gateway transport. discord.py owns reconnects, REST limits and commands."""

import asyncio
import contextlib
import io
import logging

import discord
from discord import app_commands

from ..media import normalize_image
from ..text import size_label

log = logging.getLogger(__name__)


def invite_url(application_id, guild_id=""):
    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        read_message_history=True,
        add_reactions=True,
        attach_files=True,
        embed_links=True,
        manage_channels=True,
    )
    return discord.utils.oauth_url(
        application_id,
        permissions=permissions,
        scopes=("bot", "applications.commands"),
        guild=discord.Object(id=int(guild_id)) if guild_id else discord.utils.MISSING,
    )


class DiscordTransport:
    def __init__(self, options, account_id, data_dir, emit):
        self.options, self.emit = options, emit
        self.role = account_id.rsplit(":", 1)[-1]
        self.guild_id = str(options.get("guild_id") or "")
        self.error = ""
        self.task = None
        self.logged_in = False
        self.server_verified = False
        self.interactions = {}
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.dm_messages = True
        intents.message_content = True
        self.client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.client.event(self.on_message)
        self.tree = app_commands.CommandTree(self.client)
        if self.role == "capture":
            self.add_commands()

    async def login(self):
        if not self.logged_in:
            token = self.options.get("bot_token", "").strip()
            if not token:
                raise ValueError("Discord → Bot → Reset Token → Copy, then paste the bot token here")
            try:
                await self.client.login(token)
            except discord.LoginFailure:
                raise ValueError("Discord could not verify this bot token. Copy it from your app's Bot page.") from None
            self.logged_in = True

    async def discover(self):
        await self.login()
        guilds = [{"id": str(g.id), "name": g.name} async for g in self.client.fetch_guilds(limit=None)]
        return {
            "id": str(self.client.user.id),
            "name": self.client.user.display_name,
            "guilds": guilds,
            "invite_url": invite_url(self.client.application_id, self.guild_id),
        }

    async def check(self):
        if not self.server_verified:
            result = await self.discover()
            guilds = result["guilds"]
            if not self.guild_id and len(guilds) == 1:
                self.guild_id = guilds[0]["id"]
            if not any(g["id"] == self.guild_id for g in guilds):
                raise ValueError(
                    "Choose Add to Discord, install this bot in your server, then Refresh servers and select it"
                )
            self.server_verified = True
        if self.task and self.task.done():
            if not self.task.cancelled() and self.task.exception():
                raise RuntimeError(self.connection_error(self.task.exception()))
            raise RuntimeError("Discord disconnected; reconnect this bot")
        return {
            "id": str(self.client.user.id),
            "name": self.client.user.display_name,
            "guild_id": self.guild_id,
            "capabilities": {
                "reactions": True,
                "media": True,
                "history": True,
                "threads": True,
                "native_commands": True,
                "cards": True,
            },
        }

    @staticmethod
    def connection_error(exc):
        if isinstance(exc, discord.PrivilegedIntentsRequired):
            return "Discord → Bot → Privileged Gateway Intents: turn on Message Content Intent, save, then reconnect"
        return "Discord connection failed; check your bot token, server permissions and internet connection"

    async def start(self):
        await self.check()
        self.task = asyncio.create_task(self.client.connect(reconnect=True))
        ready = asyncio.create_task(self.client.wait_until_ready())
        try:
            done, _ = await asyncio.wait((self.task, ready), timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
            if ready not in done:
                raise TimeoutError("Discord did not connect within 30 seconds")
            await ready
            if self.role == "capture":
                # Server commands are available immediately; the global command also works in DMs.
                self.tree.copy_global_to(guild=discord.Object(id=int(self.guild_id)))
                await self.tree.sync(guild=discord.Object(id=int(self.guild_id)))
                await self.tree.sync()
        except discord.PrivilegedIntentsRequired as exc:
            await self.close()
            raise ValueError(self.connection_error(exc)) from None
        except BaseException:
            await self.close()
            raise
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def on_message(self, message):
        if message.author.bot or (message.guild and str(message.guild.id) != self.guild_id):
            return
        try:
            await self.emit(
                {
                    "id": str(message.id),
                    "channel": str(message.channel.id),
                    "channel_name": message.channel.name if message.guild else "",
                    "user": str(message.author.id),
                    "user_name": message.author.display_name,
                    "text": message.content,
                    "is_dm": message.guild is None,
                    "is_mention": self.client.user in message.mentions,
                }
            )
        except Exception:
            log.exception("Discord message ingestion failed")
            self.error = "A Discord message could not be queued; check Activity before sending it again"

    def add_commands(self):
        group = app_commands.Group(name="archivebox", description="Save and find pages in your ArchiveBox")

        @group.command(name="save", description="Archive URLs with your name and this channel's tags")
        async def save(interaction: discord.Interaction, urls: str):
            await self.command(interaction, "save", urls)

        @group.command(name="search", description="Find saved pages")
        async def search(interaction: discord.Interaction, words: str):
            await self.command(interaction, "search", words)

        @group.command(name="auto", description="Automatically archive every link in this channel")
        @app_commands.choices(mode=[app_commands.Choice(name=name, value=name) for name in ("on", "off", "status")])
        async def auto(interaction: discord.Interaction, mode: str):
            await self.command(interaction, "auto", mode)

        @group.command(name="status", description="Check your archive connection")
        async def status(interaction: discord.Interaction):
            await self.command(interaction, "status")

        @group.command(name="help", description="See the ArchiveBox commands")
        async def help_command(interaction: discord.Interaction):
            await self.command(interaction, "help")

        self.tree.add_command(group)

    async def command(self, interaction, command, text=""):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id and str(interaction.guild_id) != self.guild_id:
            await interaction.edit_original_response(content="This bot is configured for another server.")
            return
        self.interactions = {k: v for k, v in self.interactions.items() if not v.is_expired()}
        self.interactions[str(interaction.id)] = interaction
        try:
            accepted = await self.emit(
                {
                    "id": str(interaction.id),
                    "channel": str(interaction.channel_id),
                    "channel_name": getattr(interaction.channel, "name", "") if interaction.guild_id else "",
                    "user": str(interaction.user.id),
                    "user_name": interaction.user.display_name,
                    "text": text,
                    "command": command,
                    "native_command": True,
                    "is_dm": interaction.guild_id is None,
                }
            )
            if not accepted:
                await self.respond(
                    str(interaction.id), "This command is disabled or you do not have access. Check bot permissions."
                )
        except Exception:
            await self.respond(str(interaction.id), "Could not queue this command. Check Activity before trying again.")
            raise

    async def respond(self, message_id, text):
        interaction = self.interactions.pop(message_id, None)
        if not interaction or interaction.is_expired():
            # Never publish an expired private slash-command response into a public channel.
            raise RuntimeError("Discord command reply expired. Run the command again to see its result.")
        result = await interaction.edit_original_response(
            content=text[:2000], allowed_mentions=discord.AllowedMentions.none()
        )
        return str(result.id)

    async def channel(self, channel):
        result = self.client.get_channel(int(channel)) or await self.client.fetch_channel(int(channel))
        guild = getattr(result, "guild", None)
        if guild and str(guild.id) != self.guild_id:
            raise ValueError("Choose a channel in this connection's Discord server")
        return result

    async def recent(self, channel, message_id, thread=""):
        target = await self.channel(channel)
        found = []
        async for item in target.history(limit=10, before=discord.Object(id=int(message_id))):
            if not item.author.bot or (self.role == "ai" and item.author.id == self.client.user.id):
                found.append(item.content)
        # Discord replies outside threads can point beyond the latest ten messages.
        current = await target.fetch_message(int(message_id))
        reference = current.reference
        if reference and reference.message_id:
            with contextlib.suppress(discord.NotFound, discord.Forbidden):
                parent = await target.fetch_message(reference.message_id)
                if not parent.author.bot and parent.content not in found:
                    found.append(parent.content)
        return list(reversed(found))

    async def react(self, channel, message_id, status):
        target = await self.channel(channel)
        message = target.get_partial_message(int(message_id))
        await message.add_reaction({"runner": "🏃", "white_check_mark": "✅", "x": "❌"}[status])
        if status != "runner":
            with contextlib.suppress(discord.NotFound):
                await message.remove_reaction("🏃", self.client.user)

    async def send(self, channel, text, thread="", media=None):
        target = await self.channel(channel)
        result = None
        # Discord limits message content to 2,000 characters; preserve the full AI answer.
        for offset in range(0, len(text) or 1, 2000):
            result = await target.send(text[offset : offset + 2000], suppress_embeds=True)
        return str(result.id)

    async def post_card(self, channel, snapshot, detail_url, media):
        target = await self.channel(channel)
        title = discord.utils.escape_markdown(" ".join((snapshot.get("title") or snapshot["url"]).split())[:100])
        persona = discord.utils.escape_markdown(str(snapshot.get("persona") or "Default"))
        line = f"✅ [{title}]({detail_url}) · <{snapshot['url']}> · Saved {size_label(snapshot.get('output_size', 0))} · 👤 {persona}"
        if favicon := media.get("favicon"):
            line += f" · [🌐]({favicon.url})"
        embed = discord.Embed(description=line, colour=0x5865F2)
        files = []
        if screenshot := media.get("screenshot"):
            files.append(
                discord.File(io.BytesIO(normalize_image(screenshot.data, "screenshot")), filename="screenshot.png")
            )
            embed.set_thumbnail(url="attachment://screenshot.png")
        result = await target.send(embed=embed, files=files)
        return str(result.id)

    async def channels(self):
        guild = self.client.get_guild(int(self.guild_id)) or await self.client.fetch_guild(int(self.guild_id))
        member = guild.me or await guild.fetch_member(self.client.user.id)
        channels = await guild.fetch_channels()
        threads = await guild.active_threads()
        return [
            {"id": str(c.id), "name": "#" + c.name, "is_dm": False}
            for c in [*channels, *threads]
            if isinstance(c, (discord.TextChannel, discord.Thread)) and c.permissions_for(member).view_channel
        ]

    async def setup_channels(self, settings):
        await self.check()
        guild = await self.client.fetch_guild(int(self.guild_id))
        existing = await guild.fetch_channels()
        result = {}
        for field, enabled in (
            ("new_channel", settings.enable_new_urls),
            ("saved_channel", settings.enable_saved_urls),
        ):
            channel_id = getattr(settings, field)
            if channel_id:
                await self.channel(channel_id)
            elif enabled:
                name = getattr(settings, field + "_name")
                channel = next((c for c in existing if isinstance(c, discord.TextChannel) and c.name == name), None)
                if not channel:
                    try:
                        channel = await guild.create_text_channel(name, reason="ArchiveBox chat setup")
                    except discord.Forbidden:
                        raise ValueError(
                            "Allow Manage Channels in the bot's Discord invite, or choose existing destinations"
                        ) from None
                    existing.append(channel)
                channel_id = str(channel.id)
            result[field] = channel_id
        return result

    async def close(self):
        await self.client.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.interactions.clear()
