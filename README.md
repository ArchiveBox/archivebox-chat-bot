<div align="center">

# 📚 ArchiveBox Chat Bot

**Your chats → your archive.**

[![Checks](https://github.com/ArchiveBox/archivebox-chat-bot/actions/workflows/check.yml/badge.svg)](https://github.com/ArchiveBox/archivebox-chat-bot/actions/workflows/check.yml) [![MIT](https://img.shields.io/badge/license-MIT-47764d)](LICENSE) ![Self hosted](https://img.shields.io/badge/self_hosted-Docker_Compose-294e40)

[ArchiveBox Bot](#-archivebox-bot) · [ArchiveBox AI Bot](#-archivebox-ai-bot) · [Setup](#connect-your-apps)

[Slack](#slack) · [Zulip](#zulip) · [Telegram](#telegram) · [WhatsApp](#whatsapp) · [IRC](#irc) · [iMessage](#imessage) · [Messenger](#messenger) · [Beeper](#beeper)

</div>

## ↗ ArchiveBox Bot

- **@mention** → save URLs from your message + the latest 10 messages.
- **DM / New URLs** → save every URL you send.
- **Groups** → optionally archive every link shared there.
- **Saved URLs** → linked title, screenshot, original URL, 🌐, **Saved 475 KB**, persona.
- **Tags** → provider + sender + group: `slack,bob,accounting`; DMs omit the group.

### Connect your apps

<details>
<summary><b>🐳 Docker → ArchiveBox URL + API key → chat provider</b></summary>

```bash
git clone https://github.com/ArchiveBox/archivebox-chat-bot.git
cd archivebox-chat-bot
docker compose up -d --build
docker compose exec archivebox-chat-bot cat /data/admin-password
```

1. **[Open setup](http://localhost:8001)** → ArchiveBox server URL + API key.
2. **Choose a provider below** → connect ArchiveBox Bot.
3. **Pick conversations** → New URLs, Saved URLs, groups, permissions.

<a href="screenshots/console.png"><img src="screenshots/console.png" width="900" alt="Actual shared setup console with connected providers and a nonblocking localhost warning"></a>

- **Reachable links:** [Tailscale](https://tailscale.com/kb/1153/enabling-https) for personal use; a domain + HTTPS for internet access.
- **Localhost / 127.0.0.1:** allowed with a warning; set **Public URL** so links work on other devices.

</details>

### Providers

#### Slack

- **Connect:** [create/install the app](https://docs.slack.dev/tools/bolt-js/creating-an-app/) → [bot token](https://docs.slack.dev/authentication/tokens/) + [app token](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/).
- **Use:** invite the bot to channels; mention it in a thread or send a DM.
- **Destinations:** create **New URLs + Saved URLs** in the console. Socket Mode needs no public webhook.

<table>
<tr><th width="33%">1 · Set up the Slack app</th><th width="33%">2 · Configure the bot</th><th width="33%">3 · Mention in a thread</th></tr>
<tr>
<td width="33%" valign="top"><a href="screenshots/slack-setup.jpg"><img src="screenshots/slack-setup.jpg" width="100%" alt="Actual Slack App Home for ArchiveBox Bot"></a></td>
<td width="33%" valign="top"><a href="screenshots/slack-config.png"><img src="screenshots/slack-config.png" width="100%" alt="Actual Slack bot settings and destination controls in the shared console"></a></td>
<td width="33%" valign="top"><a href="screenshots/slack-thread.jpg"><img src="screenshots/slack-thread.jpg" width="100%" alt="Real Slack mention saving links from the previous message with a completion reaction"></a></td>
</tr>
</table>

<table>
<tr><th width="50%">Or send a DM</th><th width="50%">Browse Saved URLs</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/slack-dm.jpg"><img src="screenshots/slack-dm.jpg" width="100%" alt="Real Slack DM saving web replay and Python references"></a></td>
<td width="50%" valign="top"><a href="screenshots/slack-saved.jpg"><img src="screenshots/slack-saved.jpg" width="100%" alt="Real Slack saved snapshots with thumbnails, Saved sizes, personas, and favicon links"></a></td>
</tr>
</table>

<details>
<summary><b>Where to get Slack tokens · same screenshots as the setup wizard</b></summary>

<table>
<tr><th width="33%">Install the app</th><th width="33%">Copy Bot User OAuth Token</th><th width="33%">Generate App-Level Token</th></tr>
<tr>
<td><a href="archivebox_chat_bot/static/guides/slack-install.png"><img src="archivebox_chat_bot/static/guides/slack-install.png" width="100%" alt="Slack's official Install to Workspace screen"></a></td>
<td><a href="archivebox_chat_bot/static/guides/slack-bot-token.png"><img src="archivebox_chat_bot/static/guides/slack-bot-token.png" width="100%" alt="Slack's official bot-token Copy screen, with the example token blurred by Slack"></a></td>
<td><a href="archivebox_chat_bot/static/guides/slack-app-token-setup.jpg"><img src="archivebox_chat_bot/static/guides/slack-app-token-setup.jpg" width="100%" alt="Our Slack app's token-generation control and connections:write scope"></a></td>
</tr>
</table>

Install/token examples: [Slack documentation](https://docs.slack.dev/tools/bolt-js/tutorials/custom-steps-workflow-builder-new/). App-level token: our installed app.

</details>

#### Zulip

- **Connect:** [create a Generic bot](https://zulip.com/help/add-a-bot-or-integration) → [copy bot email + API key](https://zulip.com/api/api-keys) → enter your server URL.
- **Use:** topics, mentions, DMs; create private **New URLs + Saved URLs** channels.

<table>
<tr><th width="33%">1 · Copy bot email</th><th width="33%">2 · Copy API key</th><th width="33%">3 · Browse Saved URLs</th></tr>
<tr>
<td width="33%" valign="top"><a href="archivebox_chat_bot/static/guides/zulip-bot-email.jpg"><img src="archivebox_chat_bot/static/guides/zulip-bot-email.jpg" width="100%" alt="Our Zulip Generic bots with real bot email addresses in the Email column"></a></td>
<td width="33%" valign="top"><a href="archivebox_chat_bot/static/guides/zulip-bot-credentials.jpg"><img src="archivebox_chat_bot/static/guides/zulip-bot-credentials.jpg" width="100%" alt="Our Generic Zulip bot with API key and configuration-download controls; secret stays hidden"></a></td>
<td width="33%" valign="top"><a href="screenshots/zulip.jpg"><img src="screenshots/zulip.jpg" width="100%" alt="Real Zulip snapshot card with uploaded screenshot and favicon"></a></td>
</tr>
</table>

#### Telegram

- **Connect:** [BotFather → /newbot](https://core.telegram.org/bots/tutorial#obtain-your-bot-token) → copy token → add to group.
- **Groups:** [disable bot privacy](https://core.telegram.org/bots/features#privacy-mode), then remove/re-add the bot for group-wide capture.
- **Commands:** `/save`, `/search`, `/auto`, `/status`, `/help`; polling needs no public webhook.

<table>
<tr><th width="33%">1 · Add to your group</th><th width="33%">2 · Choose bot preferences</th><th width="33%">3 · Save references</th></tr>
<tr>
<td width="33%" valign="top"><a href="screenshots/telegram-group-config.jpg"><img src="screenshots/telegram-group-config.jpg" width="100%" alt="Actual Telegram group with the ArchiveBox bots added"></a></td>
<td width="33%" valign="top"><a href="screenshots/telegram-config.png"><img src="screenshots/telegram-config.png" width="100%" alt="Actual Telegram capture preferences and saved destination"></a></td>
<td width="33%" valign="top"><a href="screenshots/telegram-capture.jpg"><img src="screenshots/telegram-capture.jpg" width="100%" alt="Real Telegram group receiving RFC 9111 and Python snapshots with Saved sizes"></a></td>
</tr>
</table>

<details>
<summary><b>BotFather · names & group privacy</b></summary>

<a href="archivebox_chat_bot/static/guides/telegram-botfather-name.jpg"><img src="archivebox_chat_bot/static/guides/telegram-botfather-name.jpg" width="480" alt="Our BotFather conversation setting the bot display names"></a>

<a href="archivebox_chat_bot/static/guides/telegram-botfather-capture-privacy.jpg"><img src="archivebox_chat_bot/static/guides/telegram-botfather-capture-privacy.jpg" width="480" alt="Our BotFather conversation selecting the capture bot and disabling group privacy"></a>

</details>

#### WhatsApp

- **Connect:** [Settings → Agents → Create an agent → View API key](https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf#page=3) on your primary phone.
- **Groups:** choose QR pairing → [Linked Devices → Link a Device](https://faq.whatsapp.com/1317564962315842). [Agent keys](https://faq.whatsapp.com/1050934623978152) support creator DMs only.
- **Live evidence:** Agent DM capture ✅ · linked-account groups pending.

<table>
<tr><th width="33%">1 · iPhone: create agent → API key</th><th width="33%">2 · Android: add an agent</th><th width="33%">3 · Save from a DM</th></tr>
<tr>
<td width="33%" valign="top"><a href="archivebox_chat_bot/static/guides/whatsapp-agents-ios.webp"><img src="archivebox_chat_bot/static/guides/whatsapp-agents-ios.webp" width="100%" alt="WABetaInfo iPhone example showing Create an agent, name, and View API key"></a></td>
<td width="33%" valign="top"><a href="archivebox_chat_bot/static/guides/whatsapp-agents-android.webp"><img src="archivebox_chat_bot/static/guides/whatsapp-agents-android.webp" width="100%" alt="WABetaInfo Android example showing Agents and Add an agent"></a></td>
<td width="33%" valign="top"><a href="screenshots/whatsapp-capture.jpg"><img src="screenshots/whatsapp-capture.jpg" width="100%" alt="Real WhatsApp Agent DM with captured cancellation references and screenshots"></a></td>
</tr>
</table>

Phone setup examples: [WABetaInfo iOS](https://wabetainfo.com/whatsapp-is-rolling-out-chats-with-third-party-agents-on-ios/) · [Android](https://wabetainfo.com/whatsapp-is-rolling-out-chats-with-third-party-agents/). Capture screenshot: our live test.

#### IRC

- **Connect:** [server + nickname + channels](https://libera.chat/guides/connect) → [register an account](https://libera.chat/guides/registration) → [SASL](https://libera.chat/guides/sasl).
- **Use:** mentions, private messages, auto-archive channels; saved cards use text links.

<table>
<tr><th width="50%">1 · Configure the bot</th><th width="50%">2 · Browse Saved URLs</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/irc-config.png"><img src="screenshots/irc-config.png" width="100%" alt="Actual IRC bot connection preferences in the shared console"></a></td>
<td width="50%" valign="top"><a href="screenshots/irc-capture.png"><img src="screenshots/irc-capture.png" width="100%" alt="Real IRC channel with archived Python asyncio references"></a></td>
</tr>
</table>

#### iMessage

- **Connect:** [install imsg](https://github.com/openclaw/imsg/blob/main/docs/install.md) on a Mac → [Full Disk Access + Messages Automation](https://github.com/openclaw/imsg/blob/main/docs/permissions.md).
- **Use:** existing conversations; local Mac or Docker → SSH.
- **Live evidence:** send/history/watch ✅ · incoming capture pending.

<table>
<tr><th width="50%">1 · Enable Full Disk Access</th><th width="50%">2 · Allow Messages Automation</th></tr>
<tr>
<td width="50%" valign="top"><a href="archivebox_chat_bot/static/guides/imessage-permissions-setup.jpg"><img src="archivebox_chat_bot/static/guides/imessage-permissions-setup.jpg" width="100%" alt="Our Mac with Full Disk Access enabled for the host running imsg"></a></td>
<td width="50%" valign="top"><a href="archivebox_chat_bot/static/guides/imessage-automation-setup.jpg"><img src="archivebox_chat_bot/static/guides/imessage-automation-setup.jpg" width="100%" alt="Our Mac with Messages Automation enabled"></a></td>
</tr>
</table>

<details>
<summary><b>Docker → Mac prerequisites</b></summary>

- SSH key + verified `known_hosts`, readable by container UID **1000**.
- Set the Mac's absolute `imsg` path; Compose does not provision the Mac or SSH access.

</details>

#### Messenger

- **Connect:** [Meta app + Page token](https://developers.facebook.com/docs/messenger-platform/get-started) → [HTTPS webhook](https://developers.facebook.com/docs/messenger-platform/webhooks); or an existing [Matrix bridge](https://github.com/mautrix/meta).
- **Use:** Page conversations; personal groups through Matrix.
- **Live evidence:** app/Page created ✅ · incoming capture pending.

<table>
<tr><th width="50%">1 · Create a Page</th><th width="50%">2 · Generate token & configure webhook</th></tr>
<tr>
<td width="50%" valign="top"><a href="archivebox_chat_bot/static/guides/messenger-setup.jpg"><img src="archivebox_chat_bot/static/guides/messenger-setup.jpg" width="100%" alt="Actual dedicated ArchiveBox Bot Facebook Page"></a></td>
<td width="50%" valign="top"><a href="archivebox_chat_bot/static/guides/messenger-token-setup.jpg"><img src="archivebox_chat_bot/static/guides/messenger-token-setup.jpg" width="100%" alt="Our Meta app with populated Page, Generate token button, and webhook controls"></a></td>
</tr>
</table>

<details>
<summary><b>Meta webhook</b></summary>

- Register `https://<console>/connections/<connection-id>/capture/webhook` with your matching verify token.
- Subscribe the Page to message events; use `/ai/webhook` for ArchiveBox AI Bot.

</details>

#### Beeper

- **Connect networks once:** [Beeper Desktop/Server](https://github.com/beeper/cli#2-local-beeper-server-self-hosted-managed-by-the-cli) → URL + API token → **Find my accounts**.
- **Token:** [Settings → Integrations → Approved connections → +](https://developers.beeper.com/desktop-api/auth/); allow sending. [Enable remote access](https://developers.beeper.com/desktop-api/advanced/remote-access/) for Docker.
- **Live evidence:** macOS/Docker readiness ✅ · signed-in chats, media, reactions pending.

<table>
<tr><th width="50%">1 · Create a Beeper API token</th><th width="50%">2 · Check the server</th></tr>
<tr>
<td width="50%" valign="top"><a href="archivebox_chat_bot/static/guides/beeper-token.png"><img src="archivebox_chat_bot/static/guides/beeper-token.png" width="100%" alt="Felix Krause example of Beeper API token creation; enable Allow sensitive actions for sending"></a></td>
<td width="50%" valign="top"><a href="screenshots/beeper-setup.png"><img src="screenshots/beeper-setup.png" width="100%" alt="Actual running Docker Beeper Server reporting that sign-in is still required"></a></td>
</tr>
</table>

<details>
<summary><b>Docker → Beeper address & screenshot credit</b></summary>

- Host address: `http://host.docker.internal:<port>`; container `localhost` points at the bot itself. Beeper runs separately; iMessage needs a Mac.
- Token example: [Felix Krause](https://krausefx.com/blog/openclaw-my-automation-setup), earlier Beeper UI. Enable **Allow sensitive actions** for replies (off in that image).

</details>

### Groups & commands

- Invite the bot → send a message → select the group in the console.
- Enable **Archive every link** per group or across joined groups; choose administrators under **Permissions**.

| Command | Action |
|---|---|
| `/archivebox save <URLs>` | Capture links |
| `/archivebox search <words>` | Find saved pages |
| `/archivebox auto on` / `off` | Toggle this group's automatic capture |
| `/archivebox status` | Check ArchiveBox |
| `/archivebox help` | Show commands |

<details>
<summary><b>Capture & provider details</b></summary>

- Capture depth **0**, selected persona.
- History: provider API where available, otherwise messages received while connected.
- Zulip: commands in DMs or after a mention. IRC/iMessage: commands as ordinary message text.
- Reactions/media depend on the provider; IRC/iMessage lack reliably targeted reactions, Messenger Pages use text replies.

</details>

### Administration

<details>
<summary><b>Deployment & updates</b></summary>

- Include ArchiveBox: `docker compose --profile archivebox up -d --build`.
- ArchiveBox inside Compose: `http://archivebox:5797`; on the host: `http://host.docker.internal:5797`.
- **Public URL** is the address readers open; split API/admin hosts are discovered automatically.
- Back up **bridge_data**; run one service per data volume.
- Update: `git pull --ff-only && docker compose up -d --build`.

</details>

<details>
<summary><b>Credentials & recovery</b></summary>

- **Activity** → jobs, sessions, **Recover answer**, console password.
- Pending jobs retain their original server/connection; review uncertain delivery before retrying.
- Blank credential fields preserve secrets. Protect the volume; use HTTPS for remote access.
- Revoke credentials to remove access. Removing bot data preserves snapshots and chat messages.

</details>

<details>
<summary><b>Development</b></summary>

```bash
uv sync
cd connectors && npm ci && npm run build && cd ..
uv run archivebox-chat-bot
uv run python -m pytest -xq tests/local
uv run ruff check .
```

- Python: shared capture/agent engine, queue, permissions, console.
- Transports: [Beeper API](https://developers.beeper.com/desktop-api/), [Chat SDK](https://chat-sdk.dev/docs), Slack/Zulip APIs, pydle, imsg.
- Live tests: `tests/test_*_live.py`. Capture screenshots are our real sessions. Setup examples credit their original publishers and retain their ownership; they are not live acceptance evidence.

</details>

## ✧ ArchiveBox AI Bot

- **DM / @mention** → research, capture, tag, organize, and verify.
- **Follow up** → include recent messages and previous replies.
- **ArchiveBox → Agent** → inspect each new session and tool results.
- **Existing OpenCode** → providers, credentials, tools, session database; tasks survive restarts.

### Connect the AI bot

1. **ArchiveBox AI Bot** in the console → connect a second bot/account.
2. **Trusted people** → choose who can run tasks.
3. **Agent preferences** → optional prompt → enable.

### AI providers

#### Slack AI

- **Connect:** second preconfigured Slack app; native agent status and Stop controls.
- **Shown:** plan → approval → capture HTTP caching references → tag and verify.

<table>
<tr><th width="50%">1 · Configure ArchiveBox AI Bot</th><th width="50%">2 · Complete a research task</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/slack-ai-config.jpg"><img src="screenshots/slack-ai-config.jpg" width="100%" alt="Actual Slack App Home for ArchiveBox AI Bot"></a></td>
<td width="50%" valign="top"><a href="screenshots/slack-ai-task.jpg"><img src="screenshots/slack-ai-task.jpg" width="100%" alt="Real Slack AI conversation planning, capturing, tagging, and verifying HTTP caching references"></a></td>
</tr>
</table>

#### Telegram AI

- **Connect:** second BotFather token; add ArchiveBox AI Bot to the group.
- **Shown:** capture RFC 9110 → tag existing caching references → verify six sources.

<table>
<tr><th width="50%">1 · Connect the AI bot</th><th width="50%">2 · Capture, tag & verify</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/telegram-ai-config.png"><img src="screenshots/telegram-ai-config.png" width="100%" alt="Actual Telegram AI bot connection and trusted people settings"></a></td>
<td width="50%" valign="top"><a href="screenshots/telegram-ai.jpg"><img src="screenshots/telegram-ai.jpg" width="100%" alt="Real Telegram AI task capturing RFC 9110 and verifying six caching references"></a></td>
</tr>
</table>

#### IRC AI

- **Connect:** second nickname/account on the same server.
- **Shown:** find a missing asyncio reference → capture it → answer from saved HTML.

<table>
<tr><th width="50%">1 · Connect the AI bot</th><th width="50%">2 · Research archived sources</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/irc-ai-config.png"><img src="screenshots/irc-ai-config.png" width="100%" alt="Actual IRC AI bot configuration and trusted people settings"></a></td>
<td width="50%" valign="top"><a href="screenshots/irc-ai.png"><img src="screenshots/irc-ai.png" width="100%" alt="Real IRC AI task capturing Queue documentation and explaining shutdown from saved HTML"></a></td>
</tr>
</table>

#### WhatsApp AI

- **Connect:** separate Agent API key or linked account; choose trusted people.
- **Shown:** inspect existing archives → identify a missing Fetch reference → propose capture/tagging. Execution pending.

<table>
<tr><th width="50%">1 · Connect the AI bot</th><th width="50%">2 · Review the proposed plan</th></tr>
<tr>
<td width="50%" valign="top"><a href="screenshots/whatsapp-ai-config.png"><img src="screenshots/whatsapp-ai-config.png" width="100%" alt="Actual connected WhatsApp AI bot with a trusted user selected"></a></td>
<td width="50%" valign="top"><a href="screenshots/whatsapp-ai-plan.jpg"><img src="screenshots/whatsapp-ai-plan.jpg" width="100%" alt="Real WhatsApp AI inventory and proposed capture plan awaiting approval, not a completed capture task"></a></td>
</tr>
</table>

#### Other AI providers

| Provider | Second identity | Live task evidence |
|---|---|---|
| [Zulip](#zulip) | Generic bot email + API key | Multi-step capture pending |
| [iMessage](#imessage) | Separate Mac/account | Incoming task pending |
| [Messenger](#messenger) | Page or Matrix account | Incoming task pending |
| [Beeper](#beeper) | Second network account + allowed conversations | Signed-in task pending |

<details>
<summary><b>Slack AgentExchange & Apps marketplace</b></summary>

- **Not submitted or approved.** Self-hosted apps work independently of marketplace listing.
- Review requires [10+ active workspaces](https://docs.slack.dev/changelog/2026/09/01/slack-marketplace-install-requirement/), [HTTPS events rather than Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/), support/privacy URLs, and reviewer access.
- HTTPS Events + OAuth are implemented for your own public endpoint and Slack app.

</details>
