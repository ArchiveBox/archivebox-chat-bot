import { createHash } from "node:crypto";
import { mkdirSync, chmodSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import {
  Chat,
  type Adapter,
  type Message,
  type Logger,
  type WebhookOptions,
  type AdapterPostableMessage,
  type Attachment,
} from "chat";
import { TelegramAdapter } from "@chat-adapter/telegram";
import {
  createMessengerAdapter,
  MessengerAdapter,
} from "@chat-adapter/messenger";
import { createBaileysAdapter, BaileysAdapter } from "chat-adapter-baileys";
import {
  createMatrixAdapter,
  MatrixAdapter,
} from "@beeper/chat-adapter-matrix";
import {
  extractMessageContent,
  jidNormalizedUser,
  type WAMessage,
  type AuthenticationCreds,
} from "baileys";
import { ConnectorState } from "./state.js";

process.umask(0o077);
const output = (value: unknown) =>
  process.stdout.write(`${JSON.stringify(value)}\n`);
// Dependencies may use console directly. Neither credentials nor message text belong in logs.
for (const method of [
  "log",
  "info",
  "warn",
  "error",
  "debug",
  "trace",
] as const)
  console[method] = () => {};
class SafeError extends Error {}
type Options = Record<string, unknown>;
type AccountConfig = {
  id: string;
  platform: "telegram" | "whatsapp" | "messenger";
  options: Options;
  data_dir: string;
};
type RequestMessage = {
  id: string;
  method: string;
  account?: string;
  params?: Record<string, any>;
};
const string = (
  options: Options,
  key: string,
  required = false,
): string | undefined => {
  const value = options[key];
  if (typeof value === "string" && value.trim()) return value;
  if (required) throw new SafeError(`Missing required option: ${key}`);
};
const accounts = new Map<string, Account>();
const stableJSON = (value: unknown): string =>
  JSON.stringify(value, (_key, item) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
        )
      : item,
  );

// Leave command interpretation in the shared Python engine, retaining Telegram message IDs.
class TelegramTransport extends TelegramAdapter {
  protected override handleSlashCommandUpdate(): boolean {
    return false;
  }
}
// Direct adapter dispatch avoids Chat's mention/subscription filters and error-swallowing
// handler path. The sole consumer is the durable Python inbox, acknowledged via NDJSON.
class IngressChat extends Chat {
  constructor(
    readonly owner: Account,
    adapter: Adapter,
    logger: Logger,
  ) {
    super({
      userName: string(owner.config.options, "username") ?? "archivebox",
      adapters: { [adapter.name]: adapter },
      state: owner.state,
      logger,
    });
  }
  override async processMessage(
    adapter: Adapter,
    thread: string,
    input: Message | (() => Promise<Message>),
    options?: WebhookOptions,
  ): Promise<void> {
    const task = this.owner.receive(adapter, thread, input);
    options?.waitUntil?.(task);
    return task;
  }
}
class Account {
  readonly state: ConnectorState;
  adapter!: Adapter;
  bot!: IngressChat;
  whatsappCreds?: AuthenticationCreds;
  status: "connected" | "pairing" | "error" = "error";
  detail = "Starting connector";
  closing = false;
  readonly pending = new Map<
    string,
    { promise: Promise<void>; resolve: () => void }
  >();
  constructor(readonly config: AccountConfig) {
    if (!isAbsolute(config.data_dir))
      throw new SafeError("data_dir must be absolute");
    mkdirSync(config.data_dir, { recursive: true, mode: 0o700 });
    chmodSync(config.data_dir, 0o700);
    this.state = new ConnectorState(join(config.data_dir, "connector.sqlite3"));
    chmodSync(join(config.data_dir, "connector.sqlite3"), 0o600);
  }
  setStatus(state: Account["status"], detail?: string, qr?: string) {
    if (this.closing) return;
    this.status = state;
    this.detail = detail ?? "";
    output({
      event: "status",
      account: this.config.id,
      state,
      detail,
      ...(qr ? { qr } : {}),
    });
  }
  logger(): Logger {
    const log = (level: string, message: string) => {
      if (message === "Connected to WhatsApp") this.setStatus("connected");
      else if (
        message.startsWith("Connection closed") ||
        message.startsWith("Logged out")
      )
        this.setStatus("error", "WhatsApp disconnected; check linked device");
      else if (level === "error")
        this.setStatus("error", "Connector reported a transport error");
      // Never forward arbitrary SDK strings or structured arguments containing secrets.
    };
    return {
      child: () => this.logger(),
      debug: () => {},
      info: (m) => log("info", m),
      warn: (m) => log("warn", m),
      error: (m) => log("error", m),
    };
  }
  async start() {
    const o = this.config.options;
    const logger = this.logger();
    const userName = string(o, "username");
    if (this.config.platform === "telegram") {
      const mode = string(o, "mode") ?? "polling";
      if (mode !== "polling" && mode !== "webhook")
        throw new SafeError("Telegram mode must be polling or webhook");
      this.adapter = new TelegramTransport({
        botToken: string(o, "bot_token", true),
        userName,
        mode,
        secretToken: string(o, "webhook_secret", mode === "webhook"),
        logger,
        longPolling: { dropPendingUpdates: false },
      });
    } else if (this.config.platform === "whatsapp") {
      // Baileys expects a pino-compatible logger. Silence its potentially credential-bearing output.
      const quiet = {
        level: "silent",
        child: () => quiet,
        trace() {},
        debug() {},
        info() {},
        warn() {},
        error() {},
        fatal() {},
      };
      const auth = await this.state.auth();
      this.whatsappCreds = auth.state.creds;
      this.adapter = createBaileysAdapter({
        auth,
        userName,
        logger,
        phoneNumber: string(o, "phone_number"),
        onQR: (qr) =>
          this.setStatus("pairing", "Scan in WhatsApp Linked Devices", qr),
        onPairingCode: (code) =>
          this.setStatus("pairing", `Enter pairing code: ${code}`),
        socketOptions: { logger: quiet, markOnlineOnConnect: false },
        // Baileys 2.1 has an older optional reply signature. This worker uses postMessage only.
      }) as unknown as Adapter;
    } else if (
      this.config.platform === "messenger" &&
      o.transport === "matrix"
    ) {
      this.adapter = createMatrixAdapter({
        baseURL: string(o, "homeserver", true)!,
        auth: {
          type: "accessToken",
          accessToken: string(o, "access_token", true)!,
          userID: string(o, "user_id", true),
        },
        deviceID: string(o, "device_id"),
        recoveryKey: string(o, "recovery_key"),
        userName,
        logger,
        matrixSDKLogLevel: "error",
      });
    } else if (
      this.config.platform === "messenger" &&
      (o.transport === "page" || !o.transport)
    ) {
      this.adapter = createMessengerAdapter({
        pageAccessToken: string(o, "page_access_token", true),
        appSecret: string(o, "app_secret", true),
        verifyToken: string(o, "verify_token", true),
        userName,
        logger,
      });
    } else throw new SafeError("Unsupported platform or transport");
    this.bot = new IngressChat(this, this.adapter, logger);
    await this.bot.initialize();
    if (this.adapter instanceof BaileysAdapter) {
      this.setStatus("pairing", "Connecting WhatsApp linked device");
      await this.adapter.connect();
    } else {
      if (!this.adapter.botUserId)
        throw new SafeError("Could not verify account identity");
      this.setStatus("connected");
    }
    for (const row of this.state.db
      .prepare("SELECT id,payload FROM ingress")
      .all())
      void this.dispatch(String(row.id), JSON.parse(String(row.payload)));
  }
  channel(thread: string): string {
    if (this.adapter instanceof TelegramAdapter)
      return this.adapter.decodeThreadId(thread).chatId;
    if (this.adapter instanceof BaileysAdapter)
      return (this.adapter as BaileysAdapter).decodeThreadId(thread).jid;
    if (this.adapter instanceof MatrixAdapter)
      return this.adapter.decodeThreadId(thread).roomID;
    if (this.adapter instanceof MessengerAdapter)
      return this.adapter.decodeThreadId(thread).recipientId;
    throw new SafeError("Unsupported adapter");
  }
  thread(channel: string, thread?: string): string {
    if (thread?.startsWith(`${this.adapter.name}:`)) {
      if (this.channel(thread) !== channel)
        throw new SafeError("Thread does not belong to channel");
      return thread;
    }
    if (this.adapter instanceof TelegramAdapter)
      return this.adapter.encodeThreadId({
        chatId: channel,
        ...(thread ? { messageThreadId: Number(thread) } : {}),
      });
    if (this.adapter instanceof BaileysAdapter)
      return this.adapter.encodeThreadId({ jid: channel });
    if (this.adapter instanceof MatrixAdapter)
      return this.adapter.encodeThreadId({
        roomID: channel,
        rootEventID: thread,
      });
    if (this.adapter instanceof MessengerAdapter)
      return this.adapter.encodeThreadId({ recipientId: channel });
    throw new SafeError("Unsupported adapter");
  }
  async receive(
    adapter: Adapter,
    thread: string,
    input: Message | (() => Promise<Message>),
  ) {
    if (this.closing) return;
    const m = typeof input === "function" ? await input() : input;
    if (m.author.isMe || m.author.isBot === true) return;
    if (adapter instanceof BaileysAdapter) {
      const raw = m.raw as WAMessage;
      if (raw.key.fromMe) return;
      const content = extractMessageContent(raw.message);
      const ownIds = [
        adapter.botUserId,
        this.whatsappCreds?.me?.id,
        this.whatsappCreds?.me?.lid,
      ]
        .filter((id): id is string => Boolean(id))
        .map(jidNormalizedUser);
      m.isMention = Object.values(content ?? {}).some(
        (part) =>
          typeof part === "object" &&
          part !== null &&
          "contextInfo" in part &&
          (part.contextInfo?.mentionedJid ?? []).some((jid: string) =>
            ownIds.includes(jidNormalizedUser(jid)),
          ),
      );
    }
    const channel = this.channel(thread);
    const isDM = adapter.isDM ? adapter.isDM(thread) : false;
    const deliveryId = createHash("sha256")
      .update(JSON.stringify([this.config.id, channel, m.id]))
      .digest("hex");
    const event = {
      event: "message",
      account: this.config.id,
      delivery_id: deliveryId,
      needs_dm_lookup: !adapter.isDM,
      message: {
        id: m.id,
        channel,
        source_platform: this.config.platform,
        user: m.author.userId,
        user_name: m.author.fullName || m.author.userName,
        text: m.text,
        thread,
        is_dm: isDM,
        is_mention: Boolean(m.isMention),
        is_bot: false,
      },
    };
    // Persist synchronously before any metadata lookup: some adapters dispatch without awaiting.
    this.state.db
      .prepare("INSERT OR IGNORE INTO ingress VALUES (?,?)")
      .run(deliveryId, JSON.stringify(event));
    if (adapter instanceof BaileysAdapter)
      await this.state.set(
        `sender:${channel}:${m.id}`,
        m.author.userId,
        30 * 86400_000,
      );
    this.rememberChannel(
      channel,
      isDM,
      m.raw,
      isDM ? m.author.fullName || m.author.userName : undefined,
    );
    await this.dispatch(deliveryId, event);
  }
  rememberChannel(
    channel: string,
    isDM: boolean,
    raw?: unknown,
    fallbackName?: string,
  ) {
    let name = fallbackName;
    if (
      this.adapter instanceof TelegramAdapter &&
      raw &&
      typeof raw === "object" &&
      "chat" in raw
    ) {
      const chat = raw.chat as {
        title?: string;
        first_name?: string;
        last_name?: string;
        username?: string;
      };
      name =
        chat.title ||
        [chat.first_name, chat.last_name].filter(Boolean).join(" ") ||
        chat.username ||
        name;
    }
    this.state.db
      .prepare(
        "INSERT INTO channels VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=CASE WHEN excluded.name!=excluded.id THEN excluded.name ELSE channels.name END,is_dm=excluded.is_dm",
      )
      .run(channel, name || channel, Number(isDM));
  }
  async channelMetadata(channel: string) {
    const row = this.state.db
      .prepare("SELECT id,name,is_dm FROM channels WHERE id=?")
      .get(channel);
    if (row && row.name === row.id) {
      try {
        const thread = this.thread(channel);
        const sdkChannel =
          this.adapter instanceof TelegramAdapter
            ? channel
            : (this.adapter.channelIdFromThreadId?.(thread) ?? channel);
        const info = await this.adapter.fetchChannelInfo?.(sdkChannel);
        if (info && !this.closing) {
          if (info.name) row.name = info.name;
          if (info.isDM !== undefined) row.is_dm = Number(info.isDM);
          this.state.db
            .prepare("UPDATE channels SET name=?,is_dm=? WHERE id=?")
            .run(row.name, row.is_dm, channel);
        }
      } catch {
        // Discovery is optional: preserve the observed conversation and redact provider errors.
        process.stderr.write("Connector conversation metadata unavailable\n");
      }
    }
    return row;
  }
  async channels() {
    const rows = this.state.db
      .prepare("SELECT id,name,is_dm FROM channels ORDER BY id")
      .all();
    for (const row of rows) {
      Object.assign(row, await this.channelMetadata(String(row.id)));
    }
    return {
      channels: rows.map((row) => ({ ...row, is_dm: Boolean(row.is_dm) })),
      scope: "observed_conversations",
    };
  }
  async dispatch(
    id: string,
    event: {
      needs_dm_lookup?: boolean;
      message: {
        thread: string;
        channel: string;
        is_dm: boolean;
        channel_name?: string;
        source_platform?: string;
        user?: string;
        user_name?: string;
      };
    },
  ) {
    if (event.needs_dm_lookup) {
      event.message.is_dm = Boolean(
        (await this.adapter.fetchThread(event.message.thread)).isDM,
      );
      delete event.needs_dm_lookup;
      this.state.db
        .prepare("UPDATE ingress SET payload=? WHERE id=?")
        .run(JSON.stringify(event), id);
      this.state.db
        .prepare("UPDATE channels SET is_dm=? WHERE id=?")
        .run(Number(event.message.is_dm), event.message.channel);
    }
    const channel = event.message.channel;
    const observed = await this.channelMetadata(channel);
    const name = typeof observed?.name === "string" ? observed.name : undefined;
    if (name && name !== channel) {
      if (!event.message.is_dm) event.message.channel_name = name;
      // Messenger Page messages contain only the sender ID; its DM metadata
      // comes from the SDK's real Graph profile lookup.
      if (
        this.adapter instanceof MessengerAdapter &&
        event.message.user_name === event.message.user
      )
        event.message.user_name = name;
    }
    event.message.source_platform ??= this.config.platform;
    this.state.db
      .prepare("UPDATE ingress SET payload=? WHERE id=?")
      .run(JSON.stringify(event), id);
    const { needs_dm_lookup: _, ...payload } = event;
    await this.deliver(id, payload);
  }
  deliver(id: string, event: unknown): Promise<void> {
    const existing = this.pending.get(id);
    if (existing) return existing.promise;
    let resolve!: () => void;
    const promise = new Promise<void>((done) => {
      resolve = done;
    });
    this.pending.set(id, { promise, resolve });
    output(event);
    return promise;
  }
  ack(id: string) {
    this.state.db.prepare("DELETE FROM ingress WHERE id=?").run(id);
    this.pending.get(id)?.resolve();
    this.pending.delete(id);
  }
  check() {
    return {
      state: this.status,
      detail: this.detail,
      bot_user_id: this.adapter?.botUserId,
      username: this.adapter?.userName,
      platform: this.config.platform,
      transport:
        this.config.options.transport ??
        (this.config.platform === "whatsapp"
          ? "baileys"
          : this.config.platform === "telegram"
            ? (this.config.options.mode ?? "polling")
            : "page"),
      capabilities: {
        send: true,
        media: !(this.adapter instanceof MessengerAdapter),
        reactions: !(this.adapter instanceof MessengerAdapter),
        groups: !(this.adapter instanceof MessengerAdapter),
        history: "observed_messages",
        channels: "observed_conversations",
        join: false,
      },
    };
  }
  async close() {
    if (this.closing) return;
    this.closing = true;
    // Release waiting callbacks on shutdown; retained spool entries replay on next start.
    for (const pending of this.pending.values()) pending.resolve();
    this.pending.clear();
    if (this.adapter instanceof MatrixAdapter) await this.adapter.shutdown();
    if (this.bot) await this.bot.shutdown();
    this.state.db.close();
  }
}

async function handle(req: RequestMessage) {
  const p = req.params ?? {};
  if (req.method === "ack") {
    const list = req.account
      ? [accounts.get(req.account)]
      : [...accounts.values()];
    for (const account of list) account?.ack(String(p.delivery_id));
    return { acknowledged: true };
  }
  if (req.method === "configure") {
    if (!Array.isArray(p.accounts))
      throw new SafeError("accounts must be an array");
    const desired = new Map<string, AccountConfig>();
    const directories = new Set<string>();
    for (const config of p.accounts as AccountConfig[]) {
      if (
        !config ||
        typeof config.id !== "string" ||
        !config.id ||
        desired.has(config.id)
      )
        throw new SafeError("Account IDs must be unique");
      if (typeof config.data_dir !== "string" || !isAbsolute(config.data_dir))
        throw new SafeError("data_dir must be absolute");
      const directory = resolve(config.data_dir);
      if (directories.has(directory))
        throw new SafeError("Each account requires a separate data_dir");
      directories.add(directory);
      desired.set(config.id, config);
    }
    for (const [id, account] of accounts) {
      const config = desired.get(id);
      if (!config || stableJSON(config) !== stableJSON(account.config)) {
        await account.close();
        accounts.delete(id);
      }
    }
    const result: Record<string, unknown> = {};
    for (const config of desired.values()) {
      const existing = accounts.get(config.id);
      if (existing) {
        const status = existing.check();
        if (status.state !== "error") {
          result[config.id] = status;
          continue;
        }
        // Save & Connect is the user's explicit recovery action for a failed
        // account. Recreate the SDK client from its persisted session even
        // when the connection settings themselves did not change.
        await existing.close();
        accounts.delete(config.id);
      }
      const account = new Account(config);
      accounts.set(config.id, account);
      try {
        await account.start();
      } catch (error) {
        account.setStatus("error", safeError(error));
      }
      result[config.id] = account.check();
    }
    return result;
  }
  if (req.method === "close") {
    const targets = req.account
      ? [accounts.get(req.account)]
      : [...accounts.values()];
    for (const account of targets)
      if (account) {
        await account.close();
        accounts.delete(account.config.id);
      }
    return { closed: true };
  }
  const account = accounts.get(req.account ?? "");
  if (!account) throw new SafeError("Unknown account");
  if (req.method === "check") return account.check();
  if (req.method === "channels") return account.channels();
  if (req.method === "join")
    throw new SafeError(
      "Invite the account using the platform; automatic joining is unavailable",
    );
  if (req.method === "send") {
    if (!p.channel || typeof p.text !== "string")
      throw new SafeError("send requires channel and text");
    const attachments: Attachment[] = (p.media ?? []).map(
      (file: { filename: string; mimetype: string; data: string }) => ({
        name: file.filename,
        mimeType: file.mimetype,
        data: Buffer.from(file.data, "base64"),
        type: file.mimetype.startsWith("image/")
          ? "image"
          : file.mimetype.startsWith("video/")
            ? "video"
            : file.mimetype.startsWith("audio/")
              ? "audio"
              : "file",
      }),
    );
    if (attachments.length && account.adapter instanceof MessengerAdapter)
      throw new SafeError(
        "Messenger Page adapter cannot upload media; use Matrix transport for personal Messenger media",
      );
    const message: AdapterPostableMessage = {
      markdown: p.text.replace(/\s*\r?\n\s*/g, " "),
      ...(attachments.length ? { attachments } : {}),
    };
    const sent = await account.adapter.postMessage(
      account.thread(p.channel, p.thread),
      message,
    );
    const isDM = account.adapter.isDM?.(sent.threadId) ?? false;
    account.rememberChannel(p.channel, isDM, sent.raw);
    return { id: sent.id, channel: p.channel, thread: sent.threadId };
  }
  if (req.method === "react") {
    if (account.adapter instanceof MessengerAdapter)
      throw new SafeError("Messenger Page API cannot send reactions");
    const reactions: Record<string, string> =
      account.adapter instanceof TelegramAdapter
        ? { runner: "⚡", white_check_mark: "👍", x: "👎" }
        : { runner: "🏃", white_check_mark: "✅", x: "❌" };
    if (!reactions[p.status]) throw new SafeError("Unknown reaction status");
    if (account.adapter instanceof BaileysAdapter) {
      const sender = await account.state.get<string>(
        `sender:${p.channel}:${p.message_id}`,
      );
      await account.adapter.addReaction(
        account.thread(p.channel, p.thread),
        p.message_id,
        reactions[p.status],
        sender ?? undefined,
      );
    } else
      await account.adapter.addReaction(
        account.thread(p.channel, p.thread),
        p.message_id,
        reactions[p.status],
      );
    return { ok: true };
  }
  if (req.method === "webhook") {
    if (!(
      account.adapter instanceof MessengerAdapter ||
      account.adapter instanceof TelegramAdapter
    ))
      throw new SafeError("This transport does not use webhooks");
    const tasks: Promise<unknown>[] = [];
    const response = await account.adapter.handleWebhook(
      new Request(p.url, {
        method: p.method,
        headers: p.headers,
        ...(p.method !== "GET" && p.method !== "HEAD" ? { body: p.body } : {}),
      }),
      {
        waitUntil: (task) => {
          tasks.push(task);
        },
      },
    );
    await Promise.all(tasks);
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers),
      body: await response.text(),
    };
  }
  throw new SafeError("Unknown method");
}
function safeError(error: unknown): string {
  return error instanceof SafeError
    ? error.message
    : "Connector operation failed; verify credentials, connectivity, and platform permissions";
}
// Configure/close are serialized, but ack must run concurrently with pending ingress/webhooks.
let lifecycle = Promise.resolve();
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", (line) => {
  let req: RequestMessage;
  try {
    req = JSON.parse(line);
    if (typeof req.id !== "string" || typeof req.method !== "string")
      throw new Error();
  } catch {
    output({ id: null, error: "Invalid request" });
    return;
  }
  const run = async () => {
    try {
      output({ id: req.id, result: await handle(req) });
    } catch (error) {
      output({ id: req.id, error: safeError(error) });
    }
  };
  if (req.method === "configure" || req.method === "close")
    lifecycle = lifecycle.then(run);
  else void run();
});
let stopping = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  lines.close();
  for (const account of accounts.values()) await account.close();
  process.exit(0);
}
lines.on("close", () => {
  void shutdown();
});
process.once("SIGTERM", () => {
  void shutdown();
});
process.once("SIGINT", () => {
  void shutdown();
});
process.on("unhandledRejection", () => {
  process.stderr.write(
    "Connector transport failure; restarting worker is required\n",
  );
  process.exit(1);
});
