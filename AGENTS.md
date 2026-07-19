# AGENTS.md

## Cursor Cloud specific instructions

This is a **zero-dependency Node.js** analytics suite (Solana / pump.fun). There is no
`package.json`, no `node_modules`, no build step, no linter, and no automated test suite.
The scripts rely only on native global `WebSocket` + `fetch` (Node 18+; project targets
Node 24, but the VM's Node 22 works). "Setup" is just having Node present — nothing to install.

### Running the tools
All modules are plain `node <file>` invocations, documented in `README.md` and `PORTFOLIO.md`.
- Long-running dashboards/daemons (run until Ctrl-C): `observer.js`, `multi_watch.js`,
  `rug_watch.js`, `paper_sniper.js`. When testing these non-interactively, wrap with
  `timeout <sec> node <file>` so they exit on their own.
- One-shot / finite utilities: `stats.js` (offline analyzer, exits immediately),
  `probe.js` and `decode_full.js` (dev probes, self-exit after ~30s).
- Optional config comes from a local `.env` (copy from `.env.example`); it is git-ignored.
  All runtime data is written to `logs/*.jsonl` (also git-ignored, auto-created).

### End-to-end pipeline
`multi_watch.js` is the primary collector → writes `logs/token_outcomes.jsonl`; then
`stats.js` reads that file and prints the analytics report. `stats.js` exits early with a
message if no dataset exists yet, so run `multi_watch.js` (e.g. `--quiet` for a short window)
first to generate data.

### Non-obvious caveats
- **Binance is geo-blocked** from the Cloud VM region: `api.binance.com/.../ticker/price`
  returns `"Service unavailable from a restricted location"`. This only affects the USD SOL
  price shown in dashboards (renders as `SOL ?`); all SOL-denominated metrics still work. It
  is NOT a setup failure.
- These tools stream **live mainnet data** (PumpPortal + public Solana RPC). Output is
  non-deterministic and depends on real-time on-chain activity, so exact numbers vary per run.
- The default public Solana RPC (`wss://api.mainnet-beta.solana.com`) is rate-limited; for
  long/reliable runs set `RPC_WS` to a free Helius key. Everything is read-only — no wallet,
  no keys, no transactions are ever sent.
