const $ = (s) => document.querySelector(s);
let csrf = "",
  firstRun = false,
  current,
  editing,
  role = "capture",
  dirty = false,
  beeperAccounts = [],
  beeperChats = [],
  beeperState = "",
  discordSetup = null,
  eventRows = [],
  refreshing = false,
  probing = false;
const providers = {
  beeper: {
    name: "Beeper",
    icon: "◌",
    blurb: "Many networks, one connection",
    shared: [["base_url", "Beeper Desktop or Server URL", "url"]],
    fields: [["access_token", "Beeper API access token", "password"]],
    advanced: [["account_id", "Network account ID"], ["chat_ids", "Selected conversation IDs", "list"]],
  },
  slack: {
    name: "Slack",
    icon: "#",
    blurb: "Workspaces, threads, DMs",
    advanced: [
      ["history_token", "Thread history user token", "password"],
      ["client_id", "OAuth client ID"],
      ["client_secret", "OAuth client secret", "password"],
      ["signing_secret", "Signing secret", "password"],
    ],
  },
  discord: {
    name: "Discord",
    icon: "◖◗",
    blurb: "Servers, threads, DMs",
    advanced: [],
  },
  email: {
    name: "Email",
    icon: "✉",
    blurb: "Forward or CC · inbound IMAP",
    fields: [
      ["username", "Mailbox email address", "email"],
      ["password", "Mailbox app password", "password"],
      ["host", "IMAP server"],
    ],
    advanced: [
      ["folder", "Mailbox folder"],
      ["tls_mode", "Encryption", "select", ["tls", "starttls"]],
      ["port", "IMAP port", "number"],
      ["poll_seconds", "Check for mail every (seconds)", "number"],
      ["max_message_mb", "Maximum email size (MB)", "number"],
    ],
  },
  zulip: {
    name: "Zulip",
    icon: "≋",
    blurb: "Channels, topics, DMs",
    shared: [["url", "Zulip server URL", "url"]],
    fields: [
      ["email", "Bot email", "email"],
      ["api_key", "Bot API key", "password"],
    ],
  },
  telegram: {
    name: "Telegram",
    icon: "➤",
    blurb: "Groups, topics, DMs",
    fields: [["bot_token", "Paste your BotFather token", "password"]],
    advanced: [["username", "Bot username"]],
  },
  whatsapp: {
    name: "WhatsApp",
    icon: "◉",
    blurb: "Agent key or group pairing",
    fields: [["transport", "Connection type", "select", ["agent", "baileys"]]],
    agent: [["api_key", "WhatsApp agent API key", "password"]],
    baileys: [],
    advanced: [
      ["username", "Mention name"],
      ["phone_number", "Phone number for pairing code"],
    ],
  },
  messenger: {
    name: "Messenger",
    icon: "ϟ",
    blurb: "Pages or connected Matrix groups",
    fields: [["transport", "Connection type", "select", ["page", "matrix"]]],
    page: [
      ["page_access_token", "Page access token", "password"],
      ["app_secret", "App secret", "password"],
      ["verify_token", "Webhook verify token", "password"],
    ],
    matrix: [
      ["homeserver", "Matrix homeserver", "url"],
      ["user_id", "Matrix user ID"],
      ["access_token", "Access token", "password"],
    ],
    advanced: [
      ["device_id", "Matrix device ID"],
      ["recovery_key", "Matrix recovery key", "password"],
    ],
  },
  irc: {
    name: "IRC",
    icon: "#_",
    blurb: "Channels and private messages",
    shared: [
      ["server", "IRC server"],
      ["port", "Port", "number"],
      ["tls", "Use TLS", "checkbox"],
      ["channels", "Channels to join", "list"],
    ],
    fields: [["nickname", "Bot nickname"]],
    advanced: [
      ["sasl_username", "Account name"],
      ["sasl_password", "Account password", "password"],
      ["server_password", "Server password", "password"],
    ],
  },
  imessage: {
    name: "iMessage",
    icon: "▰",
    blurb: "Messages on your Mac",
    fields: [["mention", "Mention name"]],
    advanced: [
      ["ssh_host", "Remote Mac hostname"],
      ["ssh_user", "Mac username"],
      ["ssh_port", "SSH port", "number"],
      ["ssh_identity_file", "SSH key file"],
      ["ssh_known_hosts_file", "SSH known hosts file"],
      ["imsg_path", "imsg executable"],
      ["own_handles", "Mac phone numbers or email addresses", "list"],
      ["db_path", "Messages database"],
    ],
  },
};
function el(tag, text, cls) {
  const x = document.createElement(tag);
  if (text !== undefined) x.textContent = text;
  if (cls) x.className = cls;
  return x;
}
async function api(path, options = {}) {
  const r = await fetch(path, {
    signal: AbortSignal.timeout(120000),
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...options.headers,
    },
  });
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 401) {
      $("#console").hidden = true;
      $("#login").hidden = false;
    }
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return data;
}
async function probeConsole() {
  const started = performance.now();
  let message, offline = false;
  try {
    const response = await fetch("/healthz", {cache: "no-store", signal: AbortSignal.timeout(8000)});
    const ttfb = Math.round(performance.now() - started);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (!(await response.json()).ok) throw new Error("Health check failed");
    message = `Console: online · TTFB ${ttfb} ms · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    offline = true;
    message = `Console: unreachable · ${error.name === "TimeoutError" ? "8 s timeout" : error.message}`;
  }
  document.querySelectorAll("[data-server-status]").forEach(node => {
    node.textContent = message;
    node.classList.toggle("offline", offline);
    node.title = "Browser → console HTTP time to response headers. Checked every 8 seconds.";
  });
}
function toast(text, error = false) {
  $("#toast").textContent = text;
  $("#toast").classList.toggle("error", error);
  $("#toast").hidden = false;
  setTimeout(() => ($("#toast").hidden = true), 7000);
}
async function action(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  }
}
function button(text, fn, cls = "secondary") {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = () => action(fn);
  return b;
}
function link(text, href) {
  const a = el("a", text, "button secondary");
  a.href = href;
  a.target = "_blank";
  a.rel = "noreferrer";
  return a;
}
function field(parent, spec, obj) {
  const [key, title, type = "text", choices] = spec,
    l = el("label", title),
    input = el(type === "select" ? "select" : "input");
  input.name = key;
  if (type === "select") {
    for (const choice of choices) {
      const o = el(
        "option",
        choice === "agent"
          ? "WhatsApp agent · API key"
          : choice === "baileys"
            ? "Groups & DMs · QR pairing"
            : choice === "page"
              ? "Facebook Page"
              : choice === "matrix"
                ? "Personal groups via Matrix"
                : choice === "tls" ? "TLS · usually port 993"
                : choice === "starttls" ? "STARTTLS · usually port 143"
                : choice,
      );
      o.value = choice;
      input.append(o);
    }
  } else input.type = type === "list" ? "text" : type;
  if (type === "checkbox") input.checked = Boolean(obj[key]);
  else
    input.value =
      type === "list"
        ? (obj[key] || []).join(", ")
        : (obj[key] ?? (type === "select" ? choices[0] : ""));
  if (type === "select" && !obj[key]) obj[key] = choices[0];
  if (type === "password" && obj.configured_secrets?.includes(key))
    input.placeholder = "Connected · leave blank to keep";
  if (editing?.platform === "slack" && ["bot_token", "app_token"].includes(key)) {
    const prefix = key === "bot_token" ? "xoxb-" : "xapp-";
    input.placeholder ||= `${prefix}…`;
    input.pattern = `${prefix}[A-Za-z0-9-]+`;
    input.title = `Paste the Slack token starting with ${prefix}`;
    input.required = editing[role].enabled && !obj.configured_secrets?.includes(key) &&
      (key === "bot_token" || (editing.options.transport || "socket") === "socket");
  }
  input.autocomplete = type === "password" ? "off" : "";
  input.oninput = () => {
    dirty = true;
    obj[key] =
      type === "checkbox"
        ? input.checked
        : type === "number"
          ? Number(input.value)
          : type === "list"
            ? input.value.split(/[\s,]+/).filter(Boolean)
            : input.value.trim();
    if (key === "bot_token" && editing.platform === "telegram") {
      const token = input.value.match(/\b\d{6,}:[A-Za-z0-9_-]{25,}\b/);
      if (token) obj[key] = token[0];
    }
    if (editing.platform === "email" && key === "username" && !obj.host) {
      const domain = obj[key].split("@")[1]?.toLowerCase();
      const host = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com", "fastmail.com": "imap.fastmail.com"}[domain];
      if (host) {
        obj.host = host;
        $("#connection-form").elements.host.value = host;
      }
    }
    if (editing.platform === "email" && key === "tls_mode") {
      obj.port = obj[key] === "starttls" ? 143 : 993;
      $("#connection-form").elements.port.value = obj.port;
    }
    if (editing.platform === "slack" && ["bot_token", "app_token"].includes(key)) {
      const tokens = input.value.match(/\b(?:xoxb|xapp)-[A-Za-z0-9-]+/g) || [];
      if (tokens.length) { obj[key] = ""; input.value = ""; }
      for (const token of tokens) {
        const target = token.startsWith("xoxb-") ? "bot_token" : "app_token";
        obj[target] = token;
        $("#connection-form").elements[target].value = token;
      }
    }
    if (
      (key === "transport" && ["messenger", "whatsapp", "slack"].includes(editing.platform)) ||
      (key === "enabled" && editing.platform === "slack")
    )
      renderEditor();
  };
  l.append(input);
  parent.append(l);
  return input;
}
function section(parent, title) {
  const d = el("details"),
    s = el("summary", title),
    content = el("div", undefined, "form-grid");
  d.append(s, content);
  parent.append(d);
  return content;
}
function name(c) {
  return c.name || providers[c.platform].name;
}
function status(c, r) {
  if (!c.enabled || !c[r].enabled) return {detail: "Disabled"};
  return current.connections.chat?.[c.id]?.roles?.[r] || {};
}
function statusText(s) {
  return s.state === "pairing"
    ? "Ready to pair"
    : s.ok
      ? "Connected"
      : s.error || s.detail || "Not connected";
}
function openTab(tab) {
  document
    .querySelectorAll(".page")
    .forEach((x) => (x.hidden = x.id !== `page-${tab}`));
  document
    .querySelectorAll(".nav")
    .forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("#page-name").textContent = {
    connections: "Connections",
    capture: "ArchiveBox Bot",
    ai: "ArchiveBox AI Bot",
    activity: "Activity",
  }[tab];
  location.hash = tab;
  if (tab === "activity" && current) action(() => refreshEvents());
}
function updateLocalUrlWarning() {
  const form = $("#archive-form");
  const local = (value) => {
    try {
      const host = new URL(value).hostname.toLowerCase().replace(/\.$/, "");
      return host === "localhost" || host.endsWith(".localhost") || /^127\.\d+\.\d+\.\d+$/.test(host) || host === "[::1]";
    } catch { return false; }
  };
  const server = form.elements.archivebox_url.value;
  const publicUrl = form.elements.archivebox_public_url.value || server;
  $("#local-url-warning").hidden = !local(server) && !local(publicUrl);
  $("#local-url-message").textContent = local(publicUrl)
    ? "Links posted by the bots will not open from other devices. Local testing still works."
    : "This server address only works locally. Links posted by the bots use your separate Public URL.";
}
function renderConnections() {
  for (const target of ["connected", "capture-connections", "ai-connections"])
    $("#" + target).replaceChildren();
  for (const c of current.settings.connections) {
    const p = providers[c.platform];
    const row = el("div", undefined, "connection-row");
    const info = el("div");
    info.append(
      el("h3", `${p.icon} ${name(c)}`),
      el(
        "p",
        `${statusText(status(c, c.capture.enabled ? "capture" : "ai"))} · ${c.platform === "email" ? "Inbound email" : c.ai.enabled ? "AI enabled" : "AI optional"}`,
      ),
    );
    row.append(
      info,
      button("Configure", () => edit(c.id, "capture")),
    );
    $("#connected").append(row);
    for (const r of ["capture", "ai"]) {
      if (c.platform === "email" && r === "ai") continue;
      const panel = el("article", undefined, "panel");
      const heading = el("div", undefined, "panel-heading");
      heading.append(
        el("h2", `${p.icon} ${name(c)}`),
        el("span", statusText(status(c, r)), "muted"),
        button(
          r === "ai" && !c.ai.enabled ? "Connect AI bot" : "Configure",
          () => edit(c.id, r),
        ),
      );
      panel.append(heading);
      if (c.platform === "email") {
        panel.append(el("p", `Email or CC ${c.capture.options.username || "your bot address"} → URLs saved in ArchiveBox. Check Activity for captures.`, "muted"));
      } else if (r === "capture") {
        const groups = (current.groups[c.id] || []).filter((g) => !g.is_dm);
        if (groups.length) {
          for (const g of groups) {
            const label = el("label", undefined, "toggle-row"),
              i = el("input");
            i.type = "checkbox";
            i.checked =
              g.auto_archive === null
                ? c.auto_archive_groups
                : Boolean(g.auto_archive);
            i.onchange = () =>
              action(async () => {
                await api(`/api/connections/${c.id}/groups`, {
                  method: "PUT",
                  body: JSON.stringify({
                    channel: g.channel,
                    auto_archive: i.checked,
                  }),
                });
                await refresh();
              });
            label.append(
              el("span", g.name),
              el("small", "Archive all links"),
              i,
            );
            panel.append(label);
          }
        } else
          panel.append(
            el(
              "p",
              "Add the bot to a group and send a message to see it here.",
              "muted",
            ),
          );
      } else
        panel.append(
          el(
            "p",
            `${c.ai_allowed_users.length} trusted people · sessions in ArchiveBox → Agent`,
            "muted",
          ),
        );
      $("#" + r + "-connections").append(panel);
    }
  }
}
async function edit(id, r = "capture", platform) {
  role = r;
  beeperAccounts = [];
  beeperChats = [];
  beeperState = "";
  discordSetup = null;
  editing = id
    ? structuredClone(current.settings.connections.find((c) => c.id === id))
    : {
        // getRandomValues is also available on HTTP LAN/Tailscale addresses.
        id: Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""),
        platform,
        name: providers[platform].name + (current.settings.connections.some(c => c.platform === platform) ? ` ${current.settings.connections.filter(c => c.platform === platform).length + 1}` : ""),
        enabled: true,
        options: platform === "irc" ? { port: 6697, tls: true } : platform === "beeper" ? { base_url: current.beeper_default_url } : {},
        capture: { enabled: true, options: platform === "email" ? {port: 993, tls_mode: "tls", folder: "INBOX", poll_seconds: 30, max_message_mb: 25} : {} },
        ai: { enabled: false, options: {} },
        new_channel: "",
        saved_channel: "",
        new_channel_name: "new-urls",
        saved_channel_name: "saved-urls",
        enable_new_urls: true,
        enable_saved_urls: true,
        enable_mentions: true,
        enable_dms: true,
        auto_archive_groups: false,
        upload_images: true,
        allow_guests: false,
        allowed_users: [],
        allowed_channels: [],
        admin_users: [],
        ai_allowed_users: [],
        commands: ["help", "save", "search", "status", "auto"],
      };
  if (editing.platform === "email")
    editing.capture.options = {port: 993, tls_mode: "tls", folder: "INBOX", poll_seconds: 30, max_message_mb: 25, ...editing.capture.options};
  if (r === "ai") editing.ai.enabled = true;
  editing.people = [];
  dirty = false;
  $("#setup-guide").open = !id || !status(editing, r).ok;
  renderEditor();
  $("#editor").showModal();
  if (id) {
    const results = await Promise.allSettled([
      api(`/api/connections/${id}/conversations?role=${r}`),
      api(`/api/connections/${id}/people`),
    ]);
    if (editing.id !== id || role !== r) return;
    if (results[0].status === "fulfilled") current.groups[id] = results[0].value.channels;
    else toast(results[0].reason.message, true);
    if (results[1].status === "fulfilled") editing.people = results[1].value.people;
    else toast(results[1].reason.message, true);
    if (!dirty && $("#editor").open) renderEditor();
    if (editing.platform === "discord") await action(discoverDiscord);
  }
}
function renderSetupGuide() {
  const platform = editing.platform,
    account = editing[role],
    variant = ["whatsapp", "messenger"].includes(platform)
      ? `${platform}-${account.options.transport || (platform === "whatsapp" ? "agent" : "page")}`
      : platform,
    guide = $("#setup-guide"),
    steps = el("ol", undefined, "guide-steps"),
    bot = role === "ai" ? "ArchiveBox AI Bot" : "ArchiveBox Bot";
  guide.replaceChildren(el("summary", `How to connect ${providers[platform].name}`), steps);
  for (const step of setupGuides[variant]) {
    const item = el("li"), content = el("div", undefined, "guide-step");
    content.append(el("h3", step.title), el("p", step.text.replaceAll("{bot}", bot)));
    if (step.action === "slack-create")
      content.append(link("Create Slack app ↗", `/api/manifest/${role}?create=true&connection_id=${editing.id}&transport=${editing.options.transport || "socket"}`));
    if (step.action === "discord-create")
      content.append(link("Create Discord app ↗", "https://discord.com/developers/applications"));
    const file = role === "ai" && step.aiImage ? step.aiImage : step.image;
    if (file) {
      const figure = el("figure"), zoom = el("a"), img = el("img");
      zoom.href = `/static/guides/${file}`;
      zoom.target = "_blank";
      zoom.rel = "noreferrer";
      zoom.title = "Open full-size setup screenshot";
      img.src = zoom.href;
      img.alt = step.caption;
      img.loading = "lazy";
      zoom.append(img);
      const caption = el("figcaption", step.caption);
      const enlarge = el("a", "Enlarge ↗");
      enlarge.href = zoom.href;
      enlarge.target = "_blank";
      enlarge.rel = "noreferrer";
      caption.append(" · ", enlarge);
      if (step.source) {
        const source = el("a", "Source ↗");
        source.href = step.source;
        source.target = "_blank";
        source.rel = "noreferrer";
        caption.append(" · ", source);
      }
      figure.append(zoom, caption);
      content.append(figure);
    }
    if (step.field) field(content, step.field, account.options);
    if (step.action === "discord-connect") {
      content.append(button(discordSetup ? "Refresh servers" : "Find my bot", discoverDiscord));
      if (discordSetup) {
        content.append(el("p", `✓ ${discordSetup.name}`, "inline-note"));
        content.append(link("Add to Discord ↗", discordSetup.invite_url));
        const label = el("label", "Discord server"), select = el("select");
        select.append(new Option("Choose your server", ""));
        for (const guild of discordSetup.guilds) select.append(new Option(guild.name, guild.id));
        select.value = editing.options.guild_id || "";
        select.required = true;
        select.onchange = () => { editing.options.guild_id = select.value; dirty = true; };
        label.append(select); content.append(label);
        if (!discordSetup.guilds.length)
          content.append(el("p", "Add to Discord → choose your server → Authorize. Return here and Refresh servers.", "inline-note"));
      }
    }
    if (step.webhook) {
      const base = current.settings.public_url || location.origin,
        callback = `${base.replace(/\/$/, "")}/connections/${editing.id}/${role}/webhook`;
      content.append(el("code", callback, "guide-callback"));
      content.append(button("Copy callback URL", async () => {
        await navigator.clipboard.writeText(callback);
        toast("Callback URL copied");
      }));
      if (!base.startsWith("https://"))
        content.append(el("p", "Set the Chatbot Admin Console's public HTTPS URL in Connections before registering this callback.", "guide-warning"));
    }
    const docs = el("div", undefined, "guide-docs");
    for (const [title, url] of step.docs || []) {
      const a = el("a", `${title} ↗`);
      a.href = url;
      a.target = "_blank";
      a.rel = "noreferrer";
      docs.append(a);
    }
    content.append(docs);
    item.append(content);
    steps.append(item);
  }
}
function renderEditor() {
  const p = providers[editing.platform],
    account = editing[role];
  let box = $("#editor-fields");
  box.replaceChildren();
  $(".editor-layout").classList.toggle("guided-setup", ["slack", "discord"].includes(editing.platform));
  renderSetupGuide();
  $("#editor-provider").textContent = p.name;
  $("#editor-title").textContent =
    role === "ai" ? "ArchiveBox AI Bot" : "ArchiveBox Bot";
  $("#editor-status").textContent = statusText(status(editing, role));
  $("#remove-connection").hidden = !current.settings.connections.some(
    (c) => c.id === editing.id,
  );
  if (["slack", "discord"].includes(editing.platform)) {
    if (role === "capture")
      box.append(el("p", editing.platform === "discord" ? "Connect once → New URLs + Saved URLs are created automatically. Use the bot in any channel it can access." : "Connect once → New URLs + Saved URLs are created automatically. Invite ArchiveBox Bot to any other channel you want to use.", "inline-note"));
    else
      box.append(el("p", `Use a separate ${p.name} app for ArchiveBox AI Bot. Choose who can run tasks under Trusted people below.`, "inline-note"));
    const options = el("details");
    options.open = role === "ai" || Boolean(status(editing, role).ok);
    options.append(el("summary", "Bot preferences & existing channels"));
    box.append(options);
    box = options;
  }
  const base = el("div", undefined, "form-grid");
  box.append(base);
  field(base, ["name", "Connection name"], editing);
  field(base, ["enabled", "Connect this bot", "checkbox"], account);
  for (const f of p.shared || [])
    field(base, f, Object.hasOwn(account.options, f[0]) ? account.options : editing.options);
  const guide = el("div", undefined, "button-row");
  box.append(guide);
  if (editing.platform === "slack") {
    if (account.options.client_id)
      guide.append(
        link(
          "Connect with Slack ↗",
          `/connections/${editing.id}/slack/${role}/install`,
        ),
      );
  }
  if (editing.platform === "telegram")
    guide.append(link("Open BotFather ↗", "https://t.me/BotFather"));
  if (editing.platform === "zulip" && editing.options.url)
    guide.append(
      link("Create bot ↗", editing.options.url + "/#settings/your-bots"),
    );
  if (editing.platform === "whatsapp")
    box.append(
      el(
        "p",
        (account.options.transport || "agent") === "agent"
          ? "WhatsApp → Settings → Agents → copy your agent key."
          : "Save & connect, then scan the code in WhatsApp → Linked devices.",
        "muted",
      ),
    );
  if (editing.platform === "imessage")
    box.append(
      el(
        "p",
        "Connect Messages on a Mac with imsg installed. Docker connects to that Mac over SSH.",
        "muted",
      ),
    );
  if (editing.platform === "messenger")
    box.append(
      el(
        "p",
        "Facebook Pages use Meta credentials. Personal groups require an existing Matrix bridge such as mautrix-meta.",
        "muted",
      ),
    );
  const creds = el("div", undefined, "form-grid");
  box.append(creds);
  if (!["slack", "discord"].includes(editing.platform))
    for (const f of p.fields) field(creds, f, account.options);
  if (editing.platform === "beeper") {
    guide.append(link("Open Beeper ↗", "https://www.beeper.com/download"));
    box.append(button("Find my accounts", discoverBeeper));
    if (beeperState && beeperState !== "ready")
      box.append(el("p", beeperState === "needs-login" ? "Sign in to Beeper first, then find your accounts here." : "Finish device verification and syncing in Beeper, then try again.", "inline-note"));
    const accountLabel = el("label", "Account for this bot"), select = el("select");
    select.append(new Option("Choose a connected account", ""));
    for (const a of beeperAccounts) select.append(new Option(`${a.name} · ${a.status}`, a.id));
    if (account.options.account_id && !beeperAccounts.some(a => a.id === account.options.account_id))
      select.append(new Option(account.options.account_id, account.options.account_id));
    select.value = account.options.account_id || "";
    select.onchange = () => action(async () => { account.options.account_id = select.value; account.options.chat_ids = []; dirty = true; await discoverBeeper(); });
    accountLabel.append(select); box.append(accountLabel);
    const chatLabel = el("label", "Conversations this bot can use"), chats = el("select");
    chats.multiple = true;
    for (const chat of beeperChats) { const option = new Option(chat.name, chat.id); option.selected = (account.options.chat_ids || []).includes(chat.id); chats.append(option); }
    chats.onchange = () => { account.options.chat_ids = [...chats.selectedOptions].map(o => o.value); dirty = true; };
    chatLabel.append(chats); box.append(chatLabel);
    box.append(el("p", "Only selected conversations are connected. Choose a separate network account for each bot.", "muted"));
  }
  if (["messenger", "whatsapp"].includes(editing.platform))
    for (const f of p[
      account.options.transport ||
        (editing.platform === "whatsapp" ? "agent" : "page")
    ])
      field(creds, f, account.options);
  if (status(editing, role).state === "pairing") {
    if (status(editing, role).qr) {
      const img = el("img");
      img.className = "pairing";
      img.alt = "Scan this code in WhatsApp Linked devices";
      img.src = `/api/connections/${editing.id}/${role}/pairing.png?t=${Date.now()}`;
      box.append(img);
    }
    box.append(el("p", status(editing, role).detail || "Waiting for pairing."));
  }
  const permissions = el("div", undefined, "form-grid");
  box.append(permissions);
  if (editing.platform === "email") {
    box.append(el("p", "Forward or CC this mailbox. URLs from the subject, body, quoted replies, and text/HTML/.eml attachments go to ArchiveBox. Messages stay unread; no email is sent.", "inline-note"));
    field(permissions, ["include_existing", "Import existing emails on first connection", "checkbox"], account.options);
    field(permissions, ["allowed_users", "Allowed sender addresses (optional)", "list"], editing);
    box.append(el("p", "Leave blank to accept every sender. Sender filtering uses the From header; use your mail provider's rules for authenticated sender restrictions.", "muted"));
    const advanced = section(box, "Advanced inbox settings");
    for (const f of p.advanced) field(advanced, f, account.options);
    return;
  }
  if (role === "capture") {
    for (const [key, title] of [
      ["enable_mentions", "Save links when mentioned"],
      ["enable_dms", "Save links in DMs"],
      ["enable_new_urls", "Save links from New URLs"],
      ["auto_archive_groups", "Archive every link in joined groups"],
      ["enable_saved_urls", "Post completed snapshots"],
    ])
      field(permissions, [key, title, "checkbox"], editing);
    for (const [key, title] of [
      ["new_channel", "New URLs destination"],
      ["saved_channel", "Saved URLs destination"],
    ]) {
      const label = el("label", title),
        select = el("select");
      select.append(new Option(["slack", "discord"].includes(editing.platform) ? `Create #${editing[`${key}_name`]}` : "Choose a conversation", ""));
      for (const g of current.groups[editing.id] || [])
        select.append(new Option(g.name, g.channel));
      if (
        editing[key] &&
        ![...select.options].some((o) => o.value === editing[key])
      )
        select.append(new Option(editing[key], editing[key]));
      select.value = editing[key];
      select.onchange = () => {
        editing[key] = select.value;
        dirty = true;
      };
      label.append(select);
      permissions.append(label);
    }
    if (editing.platform === "zulip")
      box.append(
        button("Create New URLs + Saved URLs channels", async () => {
          await saveConnection(false);
          await api(`/api/connections/${editing.id}/channels`, {
            method: "POST",
          });
          await refresh();
          editing = {
            ...structuredClone(
              current.settings.connections.find((c) => c.id === editing.id),
            ),
            people: editing.people,
          };
          renderEditor();
        }),
      );
  }
  const people = section(box, role === "ai" ? "Trusted people" : "Permissions");
  people.parentElement.open = role === "ai";
  const key = role === "ai" ? "ai_allowed_users" : "admin_users";
  const label = el(
      "label",
      role === "ai"
        ? "People who can use the AI bot"
        : "People who can change group settings",
    ),
    select = el("select");
  select.multiple = true;
  for (const person of editing.people) {
    const o = new Option(person.name || person.id, person.id);
    o.selected = editing[key].includes(person.id);
    select.append(o);
  }
  select.onchange = () => {
    editing[key] = [...select.selectedOptions].map((o) => o.value);
    dirty = true;
  };
  label.append(select);
  people.append(label);
  if (!editing.people.length)
    people.append(
      el(
        "p",
        "Send this bot a DM first, then reopen these settings to select your name.",
        "muted",
      ),
    );
  const advanced = section(box, "Advanced provider settings");
  for (const f of p.advanced || []) field(advanced, f, account.options);
  if (editing.platform === "slack")
    field(
      advanced,
      ["transport", "Transport", "select", ["socket", "http"]],
      editing.options,
    );
  for (const [k, t] of [
    [key, "Allowed IDs"],
    ["allowed_users", "Capture user allowlist"],
    ["allowed_channels", "Group allowlist"],
  ]) {
    const obj = { value: editing[k].join(", ") },
      input = field(advanced, ["value", t], obj);
    input.oninput = () => {
      editing[k] = input.value.split(/[\s,]+/).filter(Boolean);
      dirty = true;
    };
  }
  if (role === "capture") {
    if (["slack", "discord"].includes(editing.platform)) {
      field(advanced, ["new_channel_name", "New URLs channel name"], editing);
      field(advanced, ["saved_channel_name", "Saved URLs channel name"], editing);
    }
    field(advanced, ["new_channel", "New URLs ID"], editing);
    field(advanced, ["saved_channel", "Saved URLs ID"], editing);
    field(
      advanced,
      ["upload_images", "Include screenshot previews", "checkbox"],
      editing,
    );
    field(advanced, ["allow_guests", "Allow guests", "checkbox"], editing);
  }
}
async function discoverDiscord() {
  const id = editing.id, r = role;
  const result = await api("/api/discord/discover", {method: "POST", body: JSON.stringify({connection_id: id, role: r, options: {...editing.options, ...editing[r].options}})});
  if (editing.id !== id || role !== r) return;
  discordSetup = result;
  if (result.guilds.length === 1 && !editing.options.guild_id) editing.options.guild_id = result.guilds[0].id;
  renderEditor();
}
async function discoverBeeper() {
  const id = editing.id, r = role;
  const result = await api("/api/beeper/discover", {method: "POST", body: JSON.stringify({connection_id: id, role: r, options: {...editing.options, ...editing[r].options}})});
  if (editing.id !== id || role !== r) return;
  beeperAccounts = result.accounts;
  beeperChats = result.channels;
  beeperState = result.state;
  if (result.channels.length) current.groups[id] = result.channels.map(c => ({...c, channel:c.id}));
  renderEditor();
}
async function saveConnection(close = true) {
  const value = structuredClone(editing);
  delete value.people;
  const connections = current.settings.connections.filter(
    (c) => c.id !== value.id,
  );
  connections.push(value);
  await api("/api/settings", {
    method: "PUT",
    body: JSON.stringify({ connections }),
  });
  dirty = false;
  await refresh();
  if (["slack", "discord", "email"].includes(value.platform) && value[role].enabled && !status(value, role).ok) {
    $("#setup-guide").open = true;
    throw new Error(statusText(status(value, role)));
  }
  if (close) $("#editor").close();
  toast(value.platform === "email" && value.capture.enabled ? "Inbox connected. Email or CC your bot address to save links." : ["slack", "discord"].includes(value.platform) && value[role].enabled ? `${providers[value.platform].name} connected. Send your bot a DM to get started.` : "Connection saved.");
}
function renderJobs(jobs) {
  $("#jobs").replaceChildren();
  if (!jobs.length)
    $("#jobs").append(el("p", "No jobs.", "empty"));
  for (const job of jobs) {
    const row = el("div", undefined, "job"),
      main = el("div", undefined, "job-main");
    main.append(
      el("div", job.title || job.kind, "job-title"),
      el(
        "div",
        `${job.kind} · ${new Date(job.created_at).toLocaleString()}`,
        "job-meta",
      ),
    );
    if (job.error) main.append(el("div", job.error, "job-error"));
    row.append(el("span", job.state, "job-state " + job.state), main);
    if (job.session_url) row.append(link("Open session ↗", job.session_url));
    if (job.state === "uncertain" && job.session_id) {
      row.append(button("Recover answer", async () => {
        if (confirm("Check the conversation first. Has the agent's final answer not been delivered? This reads the existing session without repeating its task.")) {
          await api("/api/jobs/resume", {method: "POST", body: JSON.stringify({id: job.id, confirmed_answer_absent: true})});
          await refresh();
        }
      }));
    } else if (["uncertain", "failed"].includes(job.state)) {
      row.append(button("Review & retry", async () => {
        if (confirm("Have you checked the destination and confirmed no remote crawl or message was created?")) {
          await api("/api/jobs/retry", {method: "POST", body: JSON.stringify({id: job.id, confirmed_remote_absent: true})});
          await refresh();
        }
      }));
    }
    $("#jobs").append(row);
  }
}
function renderEvents() {
  const query = $("#event-search").value.toLowerCase(), level = $("#event-level").value;
  const filtered = eventRows.filter(event => (!level || event.level === level) &&
    `${event.kind} ${event.connection} ${event.role} ${event.message} ${event.status_code || ""}`.toLowerCase().includes(query));
  $("#events").replaceChildren();
  for (const event of filtered) {
    const row = el("tr", undefined, `event-${event.level}`);
    const time = el("td", new Date(event.created_at).toLocaleString());
    const source = [event.connection, event.role].filter(Boolean).join(" / ") || event.kind;
    row.append(time, el("td", event.level), el("td", source), el("td", event.message),
      el("td", event.status_code || "—"), el("td", event.elapsed_ms == null ? "—" : `${Math.round(event.elapsed_ms)} ms`));
    $("#events").append(row);
  }
  if (!filtered.length) {
    const cell = el("td", "No matching events.", "empty");
    cell.colSpan = 6;
    const row = el("tr"); row.append(cell); $("#events").append(row);
  }
  $("#event-count").textContent = `${filtered.length} / ${eventRows.length} loaded · latest first`;
}
async function refreshEvents(older = false) {
  const before = older && eventRows.length ? `?before=${eventRows.at(-1).id}` : "";
  const {events} = await api("/api/events" + before);
  if (!older && events.length === 200 && eventRows.length && events.at(-1).id > eventRows[0].id) eventRows = [];
  eventRows = [...new Map([...eventRows, ...events].map(event => [event.id, event])).values()].sort((a,b) => b.id - a.id);
  renderEvents();
  if (older || eventRows.length <= 200) $("#older-events").hidden = events.length < 200;
}
$("#event-search").oninput = renderEvents;
$("#event-level").onchange = renderEvents;
$("#older-events").onclick = () => action(() => refreshEvents(true));
async function refresh() {
  const previousStatus = editing && current ? JSON.stringify(status(editing, role)) : "";
  const state = await api("/api/state", {signal: AbortSignal.timeout(10000)});
  current = state;
  csrf = state.csrf;
  $("#login").hidden = true;
  $("#console").hidden = false;
  for (const form of ["archive-form", "ai-form"])
    if (!$("#" + form).contains(document.activeElement))
      for (const input of $("#" + form).elements) {
        if (!input.name) continue;
        input.value = state.settings[input.name] || "";
        if (
          input.type === "password" &&
          state.settings.configured_secrets.includes(input.name)
        )
          input.placeholder = "Connected · leave blank to keep";
      }
  $("#open-archive").href = state.settings.archivebox_public_url;
  const archive = state.connections.archivebox || {};
  $("#archive-status").textContent = archive.ok
    ? `Online · ${archive.snapshots.toLocaleString()} snapshots · API ${Math.round(archive.latency_ms)} ms · checked ${new Date(archive.checked_at * 1000).toLocaleTimeString()}`
    : archive.error ||
      "Bring your server URL and API key.";
  $("#archive-status").title = "Console → ArchiveBox authenticated API round trip. Checked every 30 seconds.";
  const connected = Object.values(state.connections.chat || {}).filter((c) =>
    Object.values(c.roles).some((r) => r.ok),
  ).length;
  $("#connection-badge").textContent = connected
    ? `${connected} / ${state.settings.connections.length} connections online`
    : `${state.settings.connections.length} connections · none online`;
  $("#queue-summary").textContent =
    `${state.stats.done || 0} completed · ${(state.stats.waiting || 0) + (state.stats.running || 0) + (state.stats.agent_waiting || 0)} in progress · ${state.stats.uncertain || 0} need review`;
  $("#runtime-error").hidden = !state.error;
  $("#runtime-error").textContent = state.error;
  renderConnections();
  renderJobs(state.jobs);
  updateLocalUrlWarning();
  if ($("#editor").open && !dirty && previousStatus !== JSON.stringify(status(editing, role))) renderEditor();
  if (!$("#page-activity").hidden) await refreshEvents();
}
for (const [id, p] of Object.entries(providers)) {
  const b = button("", () => edit(null, "capture", id), "provider-card");
  b.append(
    el("span", p.icon, "provider-icon"),
    el("strong", p.name),
    el("small", p.blurb),
    el("span", "+", "provider-add"),
  );
  $("#providers").append(b);
}
$("#login-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    if (firstRun && $("#password").value !== $("#confirm-password").value) {
      $("#login-error").textContent = "Passwords do not match.";
      return;
    }
    const result = await api(firstRun ? "/auth/setup" : "/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#password").value }),
    });
    csrf = result.csrf;
    $("#password").value = "";
    $("#confirm-password").value = "";
    await showLogin();
    await refresh();
  });
};
$("#archive-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const data = Object.fromEntries(new FormData(e.target));
    if (
      data.archivebox_url !== current.settings.archivebox_url &&
      data.archivebox_public_url === current.settings.archivebox_public_url &&
      !/archivebox:|host\.docker\.internal/.test(data.archivebox_url)
    )
      data.archivebox_public_url = data.archivebox_url;
    await api("/api/settings", { method: "PUT", body: JSON.stringify(data) });
    const checked = await api("/api/check/archivebox", { method: "POST" });
    await refresh();
    toast(`Connected · ${checked.snapshots} snapshots`);
  });
};
$("#archive-form").addEventListener("input", updateLocalUrlWarning);
$("#edit-public-url").onclick = () => {
  const input = $("#archive-form").elements.archivebox_public_url;
  input.closest("details").open = true;
  input.focus();
};
$("#ai-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(Object.fromEntries(new FormData(e.target))),
    });
    toast("Agent preferences saved.");
  });
};
$("#password-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    await api("/api/password", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(e.target))),
    });
    e.target.reset();
    toast("Password changed.");
  });
};
$("#connection-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const submit = $("#connect-submit");
    submit.disabled = true;
    submit.textContent = "Connecting…";
    try { await saveConnection(); }
    finally { submit.disabled = false; submit.textContent = "Save & connect →"; }
  });
};
$("#close-editor").onclick = () => $("#editor").close();
$("#remove-connection").onclick = () =>
  action(async () => {
    if (confirm("Disconnect both bots on this connection?")) {
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          connections: current.settings.connections.filter(
            (c) => c.id !== editing.id,
          ),
        }),
      });
      $("#editor").close();
      await refresh();
    }
  });
$("#logout").onclick = () =>
  action(async () => {
    await api("/auth/logout", { method: "POST" });
    $("#console").hidden = true;
    $("#login").hidden = false;
    eventRows = [];
    $("#events").replaceChildren();
  });
$("#refresh").onclick = () => action(refresh);
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => openTab(b.dataset.tab)));
openTab(
  ["capture", "ai", "activity"].includes(location.hash.slice(1))
    ? location.hash.slice(1)
    : "connections",
);
async function showLogin() {
  firstRun = (await api("/auth/setup")).required;
  $("#login-description").textContent = firstRun
    ? "Choose a password for your Chatbot Admin Console."
    : "Sign in to connect your bots.";
  $("#login-submit").textContent = firstRun ? "Create password & continue →" : "Open Chatbot Admin Console →";
  $("#password").autocomplete = firstRun ? "new-password" : "current-password";
  $("#password").minLength = firstRun ? 12 : 1;
  $("#password").placeholder = firstRun ? "At least 12 characters" : "";
  $("#confirm-password-label").hidden = !firstRun;
  $("#confirm-password").required = firstRun;
  $("#login-error").textContent = "";
}
probeConsole();
showLogin().then(() => refresh()).catch(() => {});
setInterval(() => {
  if (document.hidden) return;
  if (!probing) {
    probing = true;
    probeConsole().finally(() => probing = false);
  }
  if (!refreshing && !$("#console").hidden) {
    refreshing = true;
    action(refresh).finally(() => refreshing = false);
  }
}, 8000);
