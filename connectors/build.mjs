import { build } from "esbuild";

// Matrix's published adapter and SDK use extensionless ESM imports; resolve them at
// build time. Keep the other adapters external so their runtime assets stay intact.
await build({
  entryPoints: ["src/worker.ts"],
  outfile: "dist/worker.js",
  bundle: true,
  platform: "node",
  target: "node22",
  format: "esm",
  external: [
    "baileys",
    "chat",
    "@chat-adapter/*",
    "chat-adapter-baileys",
    "@matrix-org/matrix-sdk-crypto-wasm",
  ],
  banner: {
    js: "import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);",
  },
});
