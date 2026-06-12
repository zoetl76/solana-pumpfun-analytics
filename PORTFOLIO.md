# Real-Time Solana / pump.fun Data Engineering — Case Study

**Read-only analytics suite that ingests every pump.fun token launch and trade on Solana in real time, decodes raw on-chain events, scores rug-pull risk, and backtests strategies — built solo, zero external dependencies.**

> Looking to hire someone who can read Solana on-chain data, decode Anchor events, and ship production bots fast? This is what I do. Scroll down.

---

## What this is

A self-contained suite (Node.js) that connects to Solana mainnet, subscribes to the pump.fun program firehose, and turns raw transaction logs into structured, actionable analytics — **without paid data providers**. It was built as a research project and doubles as a working demonstration of real-time blockchain data engineering.

## Highlights (the hard parts)

- **Reverse-engineered the pump.fun `TradeEvent` Anchor event from scratch** and validated the binary layout **byte-for-byte against the official IDL**: 8-byte discriminator `sha256("event:TradeEvent")[:8]`, then a 32-field Borsh struct. Decoding pulls `mint`, `sol_amount`, `is_buy`, `user`, virtual/real reserves, fees, and the embedded `creator` at fixed offsets — verified live on real trades.
- **Real-time ingestion with no paid API**: `logsSubscribe{mentions:[program]}` over a public Solana RPC WebSocket (~60 events/s firehose), plus the free PumpPortal new-token stream — with reconnect watchdog against the 10-min idle timeout.
- **Bonding-curve pricing model**: price ∝ `(30 + real_sol_reserve)²` (constant-product, k = 30 × initial supply), used for live mark-to-market, market-cap, and graduation tracking.
- **Rug-pull scoring**: composite signal from liquidity drain, dev-dump detection (creator selling, read directly from each trade), sell pressure, sniper dominance, and same-block **bundle detection** (creator + N coordinated buyers in the creation slot).
- **Backtesting harness**: paper-trading sniper that measures realistic fill price vs. notification latency, models Jito tip auctions, and reports P&L with fee/variance breakdowns — all in simulation, zero capital.
- **Zero npm dependencies.** Native Node 24 `WebSocket` + `fetch`. Custom base58, Borsh decoding, and JSONL persistence written by hand.

## Live results (real data, ~17h window)

| Metric | Value |
|---|---|
| Tokens ingested & classified | **18,000+** |
| Measured rug/death rate | **99.1 %** |
| Measured graduation rate | **0.9 %** (matches documented ~1 %) |
| Median time-to-rug | **12 seconds** |
| Bundled launches detected | **25.7 %** |

These numbers were produced by the suite itself, live — not estimated.

## The tools

| Module | Role |
|---|---|
| `observer.js` | macro firehose: token creations + graduations, bot-spam detection |
| `rug_watch.js` | single-token microscope: every trade decoded, dev-dump & liquidity-drain alerts |
| `multi_watch.js` | parallel multi-token tracker + live rug score + Telegram alerts |
| `paper_sniper.js` | strategy backtester (latency, fees, Jito tips) — paper only |
| `stats.js` | dataset analytics over the collected JSONL |

## Tech stack

`Node.js 24` · `Solana JSON-RPC (WebSocket PubSub)` · `Anchor event / Borsh decoding` · `base58` · `bonding-curve math` · `Telegram Bot API` · `JSONL data pipelines` · no framework, no dependencies.

## What I can build for you

- **Real-time on-chain data pipelines** (Solana / pump.fun / Raydium / PumpSwap): new-token detection, trade streaming, wallet tracking, custom indexers.
- **On-chain event decoders & reverse-engineering** of undocumented programs (IDL recovery, instruction/event parsing).
- **Trading & monitoring bots**: sniper/copy-trade infrastructure, alerting (Telegram/Discord), dashboards.
- **Rug-detection & transparency tooling**, analytics dashboards, backtesting frameworks.
- Fast delivery, clean code, honest scoping — I tell you when something won't work before you pay for it.

## Contact

- **Upwork / Fiverr / Malt:** _[add your profile links]_
- **GitHub:** _[publish this repo]_
- **Telegram:** _[your handle]_

_Built as an independent research project. Read-only, no financial advice. Available for freelance and contract work._
