import { DatabaseSync } from "node:sqlite";
import { MemoryStateAdapter } from "@chat-adapter/state-memory";
import {
  BufferJSON,
  initAuthCreds,
  proto,
  type AuthenticationState,
} from "baileys";

/** SDK adapter checkpoints and authentication are durable; routing/locks belong to Python. */
export class ConnectorState extends MemoryStateAdapter {
  readonly db: DatabaseSync;
  constructor(path: string) {
    super();
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires INTEGER);
      CREATE TABLE IF NOT EXISTS ingress (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, name TEXT NOT NULL, is_dm INTEGER NOT NULL);`);
  }
  override async get<T = unknown>(key: string): Promise<T | null> {
    const row = this.db
      .prepare(
        "SELECT value FROM kv WHERE key=? AND (expires IS NULL OR expires>?)",
      )
      .get(key, Date.now());
    return row ? (JSON.parse(String(row.value)) as T) : null;
  }
  override async set<T>(key: string, value: T, ttlMs?: number): Promise<void> {
    this.db
      .prepare("INSERT OR REPLACE INTO kv VALUES (?,?,?)")
      .run(key, JSON.stringify(value), ttlMs ? Date.now() + ttlMs : null);
  }
  override async delete(key: string): Promise<void> {
    this.db.prepare("DELETE FROM kv WHERE key=?").run(key);
  }
  override async setIfNotExists(
    key: string,
    value: unknown,
    ttlMs?: number,
  ): Promise<boolean> {
    this.db
      .prepare("DELETE FROM kv WHERE key=? AND expires<=?")
      .run(key, Date.now());
    return (
      this.db
        .prepare("INSERT OR IGNORE INTO kv VALUES (?,?,?)")
        .run(key, JSON.stringify(value), ttlMs ? Date.now() + ttlMs : null)
        .changes > 0
    );
  }
  async auth(): Promise<{
    state: AuthenticationState;
    saveCreds: () => Promise<void>;
  }> {
    const read = async (key: string) => {
      const value = await this.get<string>(`auth:${key}`);
      return value ? JSON.parse(value, BufferJSON.reviver) : null;
    };
    const write = (key: string, value: unknown) =>
      this.set(`auth:${key}`, JSON.stringify(value, BufferJSON.replacer));
    const creds = (await read("creds")) ?? initAuthCreds();
    return {
      state: {
        creds,
        keys: {
          get: async (type, ids) => {
            const values: Record<string, any> = {};
            for (const id of ids) {
              const value = await read(`${type}:${id}`);
              values[id] =
                type === "app-state-sync-key" && value
                  ? proto.Message.AppStateSyncKeyData.fromObject(value)
                  : value;
            }
            return values;
          },
          set: async (values) => {
            // Each SDK key batch is atomic. No network or filesystem awaits in this transaction.
            this.db.exec("BEGIN IMMEDIATE");
            try {
              for (const [type, entries] of Object.entries(values)) {
                for (const [id, value] of Object.entries(entries ?? {})) {
                  const key = `auth:${type}:${id}`;
                  if (value)
                    this.db
                      .prepare("INSERT OR REPLACE INTO kv VALUES (?,?,NULL)")
                      .run(
                        key,
                        JSON.stringify(
                          JSON.stringify(value, BufferJSON.replacer),
                        ),
                      );
                  else this.db.prepare("DELETE FROM kv WHERE key=?").run(key);
                }
              }
              this.db.exec("COMMIT");
            } catch (error) {
              this.db.exec("ROLLBACK");
              throw error;
            }
          },
        },
      },
      saveCreds: () => write("creds", creds),
    };
  }
}
