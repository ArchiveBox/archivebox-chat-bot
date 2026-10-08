<div align="center">

# 📚 ArchiveBox Chat Bot

**Save links. Archive conversations. Put your archive to work.**

[![Checks](https://github.com/ArchiveBox/archivebox-chat-bot/actions/workflows/check.yml/badge.svg)](https://github.com/ArchiveBox/archivebox-chat-bot/actions/workflows/check.yml)
[![MIT](https://img.shields.io/badge/license-MIT-47764d)](LICENSE)
![Self hosted](https://img.shields.io/badge/self_hosted-Docker_Compose-294e40)

**Slack · Zulip · Telegram · WhatsApp · IRC · iMessage · Messenger**

[ArchiveBox Bot](#-archivebox-bot) · [ArchiveBox AI Bot](#-archivebox-ai-bot) · [Setup](#connect-your-apps)

</div>

## ↗ ArchiveBox Bot

<table>
<tr><th width="33%">💬 Mention in a thread</th><th width="33%">✉ Send a DM</th><th width="33%">▧ Browse saved snapshots</th></tr>
<tr>
<td width="33%" valign="top"><a href="screenshots/slack-thread.jpg"><img src="screenshots/slack-thread.jpg" width="100%" alt="Real Slack research thread: mention ArchiveBox to capture the URLs in the previous message"></a></td>
<td width="33%" valign="top"><a href="screenshots/slack-dm.jpg"><img src="screenshots/slack-dm.jpg" width="100%" alt="Real Slack DM: capture web replay and Python reference URLs"></a></td>
<td width="33%" valign="top"><a href="screenshots/zulip.jpg"><img src="screenshots/zulip.jpg" width="100%" alt="Real Zulip saved snapshot with uploaded screenshot and favicon"></a></td>
</tr>
</table>

- **@mention the bot** → archive URLs from the latest 10 messages and your mention.
- **DM the bot** → archive the URLs you send.
- **Post in New URLs** → archive every shared link.
- **Open Saved URLs** → linked titles, original URLs, screenshots, 🌐, sizes, and personas.
- **Enable a group** → automatically archive every link posted there.
- **Choose permissions** → people, groups, commands, and bot preferences.
- **Use several apps together** → one console, one Docker service, one ArchiveBox server.

### Connect your apps

```bash
git clone https://github.com/ArchiveBox/archivebox-chat-bot.git
cd archivebox-chat-bot
docker compose up -d --build
docker compose exec archivebox-chat-bot cat /data/admin-password
```

**[Open setup → localhost:8001](http://localhost:8001)**

1. **Connect ArchiveBox** — server URL + API key.
2. **Choose a chat app** — connect its bot or pair its account.
3. **Choose conversations** — New URLs, Saved URLs, and groups to archive.

| App | Connect | Conversations |
|---|---|---|
| **Slack** | Preconfigured app → install → bot & app tokens | Channels, threads, DMs; creates New URLs / Saved URLs |
| **Zulip** | Server URL + bot email + API key | Channels, topics, DMs; creates private New URLs / Saved URLs |
| **Telegram** | BotFather token | Existing groups, topics, DMs; disable bot privacy for group-wide capture |
| **WhatsApp** | Scan a Linked Devices QR code | Existing groups and DMs through Baileys |
| **IRC** | Server + nickname; account login when required | Existing channels and private messages |
| **iMessage** | Messages on a Mac with [imsg](https://github.com/openclaw/imsg) | Existing conversations; local Mac or SSH from Docker |
| **Messenger** | Facebook Page credentials, or an existing [Matrix bridge](https://github.com/mautrix/meta) | Page conversations; personal groups through Matrix |

> **Development in progress:** Slack and Zulip have real capture screenshots above. New-provider acceptance and screenshots are being added as their live workflows pass. iMessage needs a Mac; personal Messenger groups need a Matrix bridge.

### Groups & commands

- Invite **ArchiveBox Bot** into a group and send a message to discover it in the console.
- Choose **Archive every link** for individual groups or all joined groups.
- Select administrators by name in **Permissions** to let them change group settings from chat.

| Command | Action |
|---|---|
| `/archivebox save <URLs>` | Capture links |
| `/archivebox search <words>` | Find saved pages |
| `/archivebox auto on` / `off` | Change this group’s automatic capture |
| `/archivebox status` | Check ArchiveBox |
| `/archivebox help` | Show commands |

- **Telegram:** `/save`, `/search`, `/auto`, `/status`, `/help` also work.
- **Zulip:** DM a command or put it after a mention.
- **IRC / iMessage:** `/archivebox …` in ordinary message text.
- Captures use depth **0**, the selected persona, and tags for the **provider + sender**.
- History comes from the provider where available; other providers retain the messages received while connected.
- Reactions and media follow each provider’s capabilities. IRC and standard iMessage do not provide reliably targeted reactions; Messenger Pages have text replies.

<details>
<summary><b>ArchiveBox & deployment options</b></summary>

- Start ArchiveBox alongside the bot: `docker compose --profile archivebox up -d --build`.
- Docker network: `http://archivebox:5797`; server on host: `http://host.docker.internal:5797`.
- Set **Public URL** to the ArchiveBox address people can open.
- Standard split API/admin hostnames are discovered automatically; an internal admin override is available.
- Slack Socket Mode and Telegram polling do not need a public webhook.
- Facebook Pages, Telegram webhooks, and Slack OAuth need an HTTPS console URL.
- Back up the **bridge_data** volume: settings, credentials, conversation history, and pending jobs.
- Run one service per data volume. Update with `git pull --ff-only && docker compose up -d --build`.

</details>

<details>
<summary><b>Activity, credentials & recovery</b></summary>

- **Activity** links captures and agent sessions; uncertain remote operations require review before retrying.
- Jobs remain bound to their original server, connection, and bot identity.
- The console requires authentication and CSRF protection. Tokens are redacted from its API.
- Blank password fields preserve credentials. Protect the Docker volume and use HTTPS when exposing the console.
- Change the console password in **Activity → Console password**.
- Revoke provider credentials or disconnect the account to remove chat access.
- Removing the bot’s volume does not delete ArchiveBox snapshots or chat messages.

</details>

<details>
<summary><b>Development & real-service checks</b></summary>

```bash
uv sync
cd connectors && npm ci && npm run build && cd ..
uv run archivebox-chat-bot
uv run pytest -xq tests/local
uv run ruff check .
```

- Python owns the shared capture/agent behavior, durable queue, permissions, and console.
- [Chat SDK](https://chat-sdk.dev/docs) connects Telegram, WhatsApp, and Messenger in a supervised Node subprocess.
- Native Slack/Zulip adapters preserve their thread, card, and agent features; IRC uses pydle; iMessage uses imsg.
- Live tests use real services and persisted results. Credential schemas and environment variables are in `tests/test_*_live.py`.
- Screenshots show actual conversations and captures; no simulated bot messages.

</details>

## ✧ ArchiveBox AI Bot

<a href="screenshots/slack-ai-task.jpg"><img src="screenshots/slack-ai-task.jpg" width="640" alt="Real Slack AI task: plan an HTTP caching reference collection, capture missing pages, tag them, and verify saved files across multiple replies"></a>

- **DM or @mention the AI bot** → research, capture, organize, and verify your archive.
- **Follow up in the conversation** → include the recent messages and previous replies.
- **Open ArchiveBox → Agent** → inspect each task’s persisted session and tool results.
- **Use your existing OpenCode setup** → providers, credentials, tools, and session database.
- **Choose trusted people** → control who can start agent tasks.

### Connect the AI bot

1. Open **ArchiveBox AI Bot** in the console.
2. Choose a provider and connect its **second bot/account**.
3. Select **Trusted people** and enable it.

| Provider | AI connection |
|---|---|
| **Slack** | Second preconfigured app; native agent interface, status, and Stop |
| **Zulip** | Second bot email + API key |
| **Telegram** | Second BotFather token |
| **WhatsApp** | Second linked account |
| **IRC** | Second nickname/account |
| **iMessage** | Separate Mac/account for distinct bot identities |
| **Messenger** | Second Page or Matrix account |

- Every invocation starts a titled session in the existing ArchiveBox collection directory.
- **Agent preferences** customize the prompt; trusted users can exercise the configured agent’s tools.
- AI replies and sessions stay separate from the ArchiveBox Bot’s capture workflow.

<details>
<summary><b>Slack AgentExchange & Apps marketplace</b></summary>

| Distribution | Status |
|---|---|
| Self-hosted Slack apps | Socket Mode supported |
| HTTPS Events + OAuth | Implemented for your own public endpoint and Slack app |
| Public AgentExchange / Apps listing | **Not submitted or approved** |

- [Slack requires 10+ active workspaces during review](https://docs.slack.dev/changelog/2026/09/01/slack-marketplace-install-requirement/).
- [Marketplace apps require HTTPS events instead of Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/).
- Public submission also needs support/privacy URLs, reviewer access, and approval from Slack.
- Native Slack agent support does not publish a marketplace listing automatically.

</details>
