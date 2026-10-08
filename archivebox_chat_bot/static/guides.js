// Shared by both bot roles. Images are real provider UI; external examples retain attribution.
const setupGuides = {
  email: [
    {
      title: "Choose your bot's inbox",
      text: "Use a dedicated mailbox such as archive@your-domain.com, or a folder populated by your mail provider's forwarding rule. Email, forward, or CC that address.",
      docs: [["Gmail forwarding", "https://support.google.com/mail/answer/10957"], ["Fastmail forwarding", "https://www.fastmail.help/hc/en-us/articles/360058753434-Set-up-mail-forwarding"]],
    },
    {
      title: "Connect with an app password",
      text: "Enter the mailbox address, IMAP server, and its app password. TLS on port 993 is selected for you. Gmail, iCloud, and Fastmail addresses fill their server automatically.",
      image: "email-settings.jpg",
      caption: "Connected test inbox · use your provider's server and port",
      docs: [["Gmail app passwords", "https://support.google.com/accounts/answer/185833"], ["iCloud IMAP settings", "https://support.apple.com/en-us/102525"], ["Fastmail IMAP settings", "https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports"]],
    },
    {
      title: "Email your first links",
      text: "Save & connect → email or forward a conversation → open Activity or ArchiveBox. New mail is checked every 30 seconds. Enable Import existing emails before connecting to include older mail.",
      docs: [["Gmail IMAP access", "https://support.google.com/mail/answer/7126229"]],
    },
  ],
  discord: [
    {
      title: "Create your Discord bot",
      text: "New Application → name it {bot} → Create. Open Bot → Reset Token → Copy, then paste it here.",
      action: "discord-create",
      field: ["bot_token", "Discord bot token", "password"],
      image: "discord-bot-token.jpg",
      caption: "Our Discord bot · Reset Token and token-copy instructions",
      docs: [["Create an app & token", "https://docs.discord.com/developers/quick-start/getting-started"]],
    },
    {
      title: "Enable message access",
      image: "discord-message-content.jpg",
      caption: "Our Discord bot · Message Content Intent enabled",
      text: "Bot → Privileged Gateway Intents → Message Content Intent → Save Changes. This lets the bot find URLs in earlier messages and archive channel links.",
      docs: [["Message Content Intent", "https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents"]],
    },
    {
      title: "Add to your server",
      image: "discord-install.jpg",
      caption: "Our Discord installation · scoped channel and message permissions",
      text: "Find my bot → Add to Discord → choose your server → Authorize. Refresh servers, select your server, then Save & connect. New URLs + Saved URLs are created automatically; no public webhook needed.",
      action: "discord-connect",
      docs: [["Install in a server", "https://docs.discord.com/developers/quick-start/getting-started#step-3-installing-your-app"]],
    },
  ],
  slack: [
    {
      title: "Create and install your Slack app",
      text: "Choose your workspace → Create → Install to Workspace → Allow. Permissions, events, and commands are already filled in.",
      action: "slack-create",
      image: "slack-install.png",
      caption: "Install to Workspace · Slack documentation",
      source: "https://docs.slack.dev/tools/bolt-js/tutorials/custom-steps-workflow-builder-new/",
      docs: [["Slack app setup", "https://docs.slack.dev/tools/bolt-js/creating-an-app/"]],
    },
    {
      title: "Copy the bot token",
      text: "OAuth & Permissions → Bot User OAuth Token → Copy. Paste it here.",
      field: ["bot_token", "Bot token · starts with xoxb-", "password"],
      image: "slack-bot-token.png",
      caption: "Bot User OAuth Token · Slack documentation (example token blurred by Slack)",
      source: "https://docs.slack.dev/tools/bolt-js/tutorials/custom-steps-workflow-builder-new/",
      docs: [["Slack token types", "https://docs.slack.dev/authentication/tokens/"]],
    },
    {
      title: "Generate the app token",
      text: "Basic Information → App-Level Tokens → Generate Token and Scopes. Name it ArchiveBox, add connections:write → Generate → copy and paste here.",
      field: ["app_token", "App token · starts with xapp-", "password"],
      image: "slack-app-token-setup.jpg",
      caption: "Our installed Slack app · app-level token controls",
      docs: [["Socket Mode setup", "https://docs.slack.dev/tools/python-slack-sdk/socket-mode/"]],
    },
  ],
  zulip: [
    {
      title: "Create a Generic bot",
      image: "zulip-bot-email.jpg",
      caption: "Our Zulip Bots list · Generic bot type and Email column",
      text: "In your Zulip organization: Personal settings → Bots → Add a new bot. Choose Generic and name it {bot}.",
      docs: [["Add a bot", "https://zulip.com/help/add-a-bot-or-integration"], ["Bot types", "https://zulip.com/help/bots-overview"]],
    },
    {
      title: "Copy the bot email and API key",
      text: "Personal settings → Bots: copy the bot's Email column. In Manage bot, open API key to copy the secret. Or download Zulip configuration and use its email, key, and site values. Do not use the numeric User ID.",
      image: "zulip-bot-credentials.jpg",
      caption: "Our Generic bot · API key and Zulip configuration download controls",
      docs: [["Find bot credentials", "https://zulip.com/api/api-keys"]],
    },
    {
      title: "Choose where to archive",
      text: "Save & connect, then create New URLs + Saved URLs or invite the bot to an existing channel. DM the bot to make your name available in permissions.",
      docs: [["Zulip bot setup", "https://zulip.com/help/add-a-bot-or-integration"]],
    },
  ],
  telegram: [
    {
      title: "Create your bot in BotFather",
      text: "Open the verified @BotFather → send /newbot. Name it {bot}, choose a unique username ending in bot, then copy the token from BotFather's reply.",
      image: "telegram-botfather-name.jpg",
      caption: "Our BotFather conversation · bot display names",
      docs: [["Open BotFather", "https://t.me/BotFather"], ["Get your bot token", "https://core.telegram.org/bots/tutorial#obtain-your-bot-token"]],
    },
    {
      title: "Allow group context",
      text: "For group-wide capture: /setprivacy → choose your bot → Disable. Remove and re-add the bot to existing groups after changing privacy.",
      image: "telegram-botfather-capture-privacy.jpg",
      aiImage: "telegram-botfather-privacy.jpg",
      caption: "Our BotFather conversation · group privacy disabled",
      docs: [["Telegram privacy mode", "https://core.telegram.org/bots/features#privacy-mode"]],
    },
    {
      title: "Start the conversation",
      text: "Paste the token here and Save & connect. Open the bot's DM → Start, or add it to a group. Send a message, then reopen settings to choose that conversation.",
      docs: [["Telegram bot features", "https://core.telegram.org/bots/features"]],
    },
  ],
  "whatsapp-agent": [
    {
      title: "Create an agent on your phone",
      text: "On your primary phone: WhatsApp → Settings → Agents → Create an agent. Name it {bot}. This feature is only available on eligible accounts; WhatsApp Web cannot create agents.",
      image: "whatsapp-agents-android.webp",
      caption: "Android example · Agents → add a named agent. Screenshot: WABetaInfo.",
      source: "https://wabetainfo.com/whatsapp-is-rolling-out-chats-with-third-party-agents/",
      docs: [["WhatsApp Agents help", "https://faq.whatsapp.com/1050934623978152"]],
    },
    {
      title: "Copy the agent's API key",
      text: "Open the new agent chat → View API key, or tap its name → Chat info → API key. Copy it into WhatsApp agent API key here. Each bot needs its own agent key.",
      image: "whatsapp-agents-ios.webp",
      caption: "iPhone example · View API key in the new agent chat (rightmost screen). Screenshot: WABetaInfo.",
      source: "https://wabetainfo.com/whatsapp-is-rolling-out-chats-with-third-party-agents-on-ios/",
      docs: [["Official setup steps · page 3", "https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf#page=3"]],
    },
    {
      title: "Connect and send a DM",
      text: "Save & connect, then message your agent. Agent keys work only for the creator's DMs. For existing groups, choose Groups & DMs · QR pairing instead.",
      docs: [["Agent availability and limits", "https://faq.whatsapp.com/1050934623978152"]],
    },
  ],
  "whatsapp-baileys": [
    {
      title: "Open Linked Devices on your phone",
      text: "iPhone: Settings → Linked Devices → Link a Device. Android: ⋮ → Linked devices → Link a device. Use a dedicated account for each bot.",
      docs: [["WhatsApp device linking", "https://faq.whatsapp.com/1317564962315842"], ["Watch WhatsApp's phone walkthrough", "https://www.youtube.com/watch?v=AsWJu8mOvVY&t=43s"]],
    },
    {
      title: "Scan this bot's QR code",
      text: "Choose Save & connect. Scan the QR shown here using your phone's Link a Device screen; wait for Connected. No Agent API key is needed for this mode.",
      docs: [["WhatsApp linked devices", "https://faq.whatsapp.com/1317564962315842"]],
    },
    {
      title: "Choose your groups",
      text: "Add this account to your existing group and send a message. Reopen settings to select destinations and enable automatic archiving for that group.",
      docs: [["About linked devices", "https://faq.whatsapp.com/1317564962315842"]],
    },
  ],
  irc: [
    {
      title: "Choose a network and bot nickname",
      text: "Use your network's server, TLS port, and channels. For example: irc.libera.chat, port 6697, TLS on, and #your-channel. Give {bot} its own nickname.",
      image: "irc-setup.png",
      caption: "Our test network in The Lounge · use your own network's address and channels",
      docs: [["Libera connection guide", "https://libera.chat/guides/connect"]],
    },
    {
      title: "Register the bot account",
      text: "Follow your network's NickServ registration flow and email verification. Enter the account name/password under Advanced provider settings → Account name / Account password.",
      docs: [["Account registration", "https://libera.chat/guides/registration"], ["SASL login", "https://libera.chat/guides/sasl"]],
    },
    {
      title: "Join and test",
      text: "Save & connect, then mention the bot in one of its channels or send it a private message. Choose New URLs and Saved URLs from the channel list.",
      docs: [["Network-specific requirements", "https://libera.chat/guides/registration"]],
    },
  ],
  imessage: [
    {
      title: "Prepare Messages on a Mac",
      text: "Sign in to Messages on the Mac that will run the bot. Install imsg using its installation guide. A distinct second bot needs a separate Mac/account.",
      docs: [["Install imsg", "https://github.com/openclaw/imsg/blob/main/docs/install.md"]],
    },
    {
      title: "Allow message access",
      text: "System Settings → Privacy & Security → Full Disk Access: enable the app running imsg (for example Terminal). Under Automation, allow that app to control Messages.",
      image: "imessage-permissions-setup.jpg",
      caption: "Our test Mac · enable your own terminal/service host, shown here as ChatGPT",
      docs: [["macOS permission steps", "https://github.com/openclaw/imsg/blob/main/docs/permissions.md"]],
    },
    {
      title: "Connect the Mac",
      image: "imessage-automation-setup.jpg",
      caption: "Our test Mac · Messages Automation enabled for its host app",
      text: "Running here on the Mac? Use its imsg executable path. From Docker, enable General → Sharing → Remote Login on the Mac and enter SSH host/user/key/known-hosts paths under Advanced provider settings.",
      docs: [["Remote Login on Mac", "https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac"]],
    },
  ],
  "messenger-page": [
    {
      title: "Create an app and connect your Page",
      text: "In Meta for Developers: add the Messenger use case → Customize → Messenger API Settings. Under Generate access tokens, add your Facebook Page.",
      image: "messenger-token-setup.jpg",
      caption: "Our Meta app · connected Page, Generate token button, and webhook controls",
      docs: [["Meta Messenger setup", "https://developers.facebook.com/docs/messenger-platform/get-started"]],
    },
    {
      title: "Copy the Page token and App Secret",
      text: "Messenger API Settings → Generate Token: copy Page access token. App Settings → Basic → App Secret: reveal and copy into App secret here.",
      docs: [["Messenger credentials", "https://developers.facebook.com/docs/messenger-platform/get-started"]],
    },
    {
      title: "Connect the message webhook",
      text: "Use your Chatbot Admin Console's public HTTPS address for the callback below. Choose a verify token and enter the same value here and in Meta. Save here, then verify in Meta and subscribe the Page to message events.",
      webhook: true,
      docs: [["Meta webhook setup", "https://developers.facebook.com/docs/messenger-platform/webhooks"]],
    },
  ],
  "messenger-matrix": [
    {
      title: "Connect Messenger to your Matrix bridge",
      text: "Use an existing mautrix-meta bridge and follow its Messenger login flow. This mode connects personal conversations; it does not use Facebook Page credentials.",
      docs: [["Bridge setup", "https://docs.mau.fi/bridges/go/meta/index.html"]],
    },
    {
      title: "Copy the Matrix account details",
      text: "Enter the Matrix homeserver URL, user ID, and that account's access token. Use the device ID/recovery key in Advanced provider settings when required by your bridge.",
      docs: [["Bridge authentication", "https://docs.mau.fi/bridges/go/meta/authentication.html"]],
    },
    {
      title: "Choose bridged conversations",
      text: "Save & connect, send a message in a bridged conversation, then reopen settings to choose it. Each bot needs a distinct Matrix account.",
      docs: [["mautrix-meta documentation", "https://docs.mau.fi/bridges/go/meta/index.html"]],
    },
  ],
  beeper: [
    {
      title: "Connect your networks in Beeper",
      text: "Sign in to Beeper Desktop or Server and connect your chat accounts. On Desktop: Settings → Integrations → enable the local API.",
      docs: [["Enable the Beeper API", "https://developers.beeper.com/desktop-api/"], ["Headless server", "https://github.com/beeper/cli#2-local-beeper-server-self-hosted-managed-by-the-cli"]],
    },
    {
      title: "Create an API access token",
      text: "Integrations → Approved connections → +. Name the token {bot}, allow sending messages (Allow sensitive actions), create it, and paste it here.",
      image: "beeper-token.png",
      caption: "Earlier Beeper token dialog · enable Allow sensitive actions for replies. Screenshot: Felix Krause.",
      source: "https://krausefx.com/blog/openclaw-my-automation-setup",
      docs: [["Beeper authentication", "https://developers.beeper.com/desktop-api/auth/"]],
    },
    {
      title: "Find accounts and select conversations",
      text: "Enter Beeper's API address → Find my accounts → select this bot's account and allowed chats. Docker/another host also needs Integrations → Advanced → Remote Access; use a reachable host address, not container localhost.",
      docs: [["Remote access settings", "https://developers.beeper.com/desktop-api/advanced/remote-access/"]],
    },
  ],
};
