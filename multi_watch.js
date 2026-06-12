// multi_watch.js — Suit EN PARALLÈLE tous les tokens pump.fun frais, calcule un SCORE DE RUG (0-100),
// détecte les LANCEMENTS EN BUNDLE (créateur + snipers dans le même bloc) et ALERTE sur les SURVIVANTS.
// Lecture seule, 0 capital, 0 clé obligatoire, 0 transaction.
//
// Naissances : WS gratuit PumpPortal (subscribeNewToken). Trades : 1 souscription firehose RPC
// (logsSubscribe{mentions:[programme pump.fun]}), décodés et routés vers les tokens suivis.
//
//   node multi_watch.js
//   RPC_WS="wss://mainnet.helius-rpc.com/?api-key=CLE"  node multi_watch.js   # 24/7 fiable
//   NTFY_TOPIC="mon-topic-secret"                        node multi_watch.js   # alerte survivants sur tél (ntfy.sh)

const fs = require("fs");
const path = require("path");

// charge un .env local (KEY=VALUE par ligne) dans process.env si présent — sans dépendance
try {
  for (const line of fs.readFileSync(path.join(__dirname, ".env"), "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch {}

const PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const TRADE_DISC = Buffer.from([189, 219, 127, 211, 78, 230, 97, 238]);
const RPC_WS = process.env.RPC_WS || "wss://api.mainnet-beta.solana.com";
const PUMPPORTAL_WS = "wss://pumpportal.fun/api/data";
const NTFY_TOPIC = process.env.NTFY_TOPIC || null;
const TG_TOKEN = process.env.TG_TOKEN || null;   // token bot Telegram (@BotFather)
const TG_CHAT = process.env.TG_CHAT || null;     // chat_id de la conversation avec le bot

const REFRESH_MS = 4000;
const SNIPER_WINDOW_S = 15;
const BUNDLE_MIN = 4;            // ≥4 acheteurs distincts dans le bloc de création = bundle
const BUNDLE_SLOT_WINDOW = 1;    // bloc de création + 1 bloc suivant
const SURVIVOR_MIN_S = Number(process.env.SURVIVOR_MIN_S || 600); // durée de survie pour être "survivant" (réglable via env)
const SURVIVOR_MIN_REALSOL = Number(process.env.SURVIVOR_MIN_REALSOL || 0); // ne pinger que si le pic de réserve réelle a dépassé ce seuil (filtre traction)
const DEAD_AFTER_S = 180;        // pas de trade depuis 3 min = mort
const EVICT_AFTER_S = 1800;      // retiré de la table 30 min après la mort
const MAX_ROWS = 16;

const LOG_DIR = path.join(__dirname, "logs");
fs.mkdirSync(LOG_DIR, { recursive: true });
const OUTCOMES = path.join(LOG_DIR, "token_outcomes.jsonl");
const SURVIVORS = path.join(LOG_DIR, "survivors.jsonl");

// ---- base58 ----
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function bs58(buf) { let n = 0n; for (const b of buf) n = n * 256n + BigInt(b); let s = ""; while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; } for (const b of buf) { if (b === 0) s = "1" + s; else break; } return s; }
const shrt = (a) => a ? a.slice(0, 4) + "…" + a.slice(-4) : "?";

// ---- décodage (offsets fixes validés vs IDL officiel) ----
function tradeMint(buf) { if (buf.length < 209 || !buf.subarray(0, 8).equals(TRADE_DISC)) return null; return bs58(buf.subarray(8, 40)); }
function decodeTrade(buf) {
  return {
    solAmount: Number(buf.readBigUInt64LE(40)) / 1e9,
    isBuy: buf.readUInt8(56) === 1,
    user: bs58(buf.subarray(57, 89)),
    ts: Number(buf.readBigInt64LE(89)),
    realSol: Number(buf.readBigUInt64LE(113)) / 1e9,
    creator: bs58(buf.subarray(177, 209)),
  };
}

// ---- état ----
const tracked = new Map();
const life = { born: 0, dead: 0, rug: 0, graduated: 0, bundled: 0, survivors: 0 };
let solUsd = null, lastMsgAt = Date.now();
const now = () => Date.now();
const nowSec = () => Math.floor(Date.now() / 1000);

function newToken(mint, m) {
  return {
    mint, symbol: m?.symbol || "?", name: m?.name || "", creator: m?.traderPublicKey || null,
    bornSec: nowSec(), firstTradeTs: null, lastTradeTs: null,
    buys: 0, sells: 0, solIn: 0, solOut: 0, peakRealSol: 0, lastRealSol: 0,
    devSoldSol: 0, devBoughtSol: m?.solAmount || 0, snipers: new Set(),
    creationSlot: null, slotBuyers: new Map(), bundled: false, bundleSize: 0, bundleSol: 0, bundleDone: false,
    graduated: false, classified: null, survivorAlerted: false,
  };
}

// ---- prix SOL + ntfy ----
async function refreshSol() { try { const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", { signal: AbortSignal.timeout(8000) }); const j = await r.json(); if (j?.price) solUsd = parseFloat(j.price); } catch {} }
const usd = (sol) => solUsd && sol != null ? `$${(sol * solUsd).toLocaleString("en", { maximumFractionDigits: 0 })}` : `${(sol ?? 0).toFixed(1)}◎`;
async function notify(title, msg) {
  try {
    if (TG_TOKEN && TG_CHAT) {
      await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: TG_CHAT, text: `${title}\n${msg}`, disable_web_page_preview: true }),
        signal: AbortSignal.timeout(8000),
      });
    } else if (NTFY_TOPIC) {
      await fetch(`https://ntfy.sh/${NTFY_TOPIC}`, { method: "POST", headers: { Title: title }, body: msg, signal: AbortSignal.timeout(8000) });
    }
  } catch {}
}

// ---- score de rug ----
function rugScore(t) {
  let s = 0;
  if (t.peakRealSol >= 1) s += Math.min(30, Math.max(0, 1 - t.lastRealSol / t.peakRealSol) * 100 * 0.3);
  if (t.devSoldSol > 0) s += Math.min(35, 20 + t.devSoldSol * 5);
  const tot = t.buys + t.sells;
  if (tot >= 5) { const sr = t.sells / tot; if (sr > 0.55) s += Math.min(22, (sr - 0.55) * 100 * 0.6); }
  if (t.buys >= 3 && t.snipers.size / Math.max(1, t.buys) > 0.5) s += 6;
  if (t.bundled) s += 10;
  return Math.min(100, Math.round(s));
}
function statusOf(t) {
  if (t.graduated) return ["🎓", "GRADUÉ"];
  if (t.devSoldSol > 0 || (t.peakRealSol >= 1 && t.lastRealSol / t.peakRealSol < 0.3)) return ["🔴", "RUG"];
  const idle = t.lastTradeTs ? nowSec() - t.lastTradeTs : nowSec() - t.bornSec;
  if (idle > DEAD_AFTER_S) return ["⚰️", t.lastTradeTs ? "MORT" : "MORT-NÉ"];
  if ((nowSec() - t.bornSec) >= SURVIVOR_MIN_S && t.lastRealSol >= 1 && idle < 120 && t.peakRealSol >= SURVIVOR_MIN_REALSOL) return ["⭐", "SURVIVANT"];
  if (rugScore(t) >= 40) return ["🟡", "SUSPECT"];
  return ["🟢", "VIVANT"];
}

function classifyOnce(t) {
  const [, label] = statusOf(t);
  if (["RUG", "MORT", "MORT-NÉ", "GRADUÉ"].includes(label) && t.classified !== label) {
    if (t.classified === null) { if (label === "GRADUÉ") life.graduated++; else if (label === "RUG") life.rug++; else life.dead++; }
    t.classified = label;
    try { fs.appendFileSync(OUTCOMES, JSON.stringify({ t: now(), mint: t.mint, symbol: t.symbol, outcome: label, score: rugScore(t), peakRealSol: +t.peakRealSol.toFixed(2), buys: t.buys, sells: t.sells, ageS: nowSec() - t.bornSec, devSoldSol: +t.devSoldSol.toFixed(3), bundled: t.bundled, bundleSize: t.bundleSize }) + "\n"); } catch {}
  }
}

// ---- traitement d'un trade (avec slot pour la détection de bundle) ----
function onTrade(mint, buf, slot) {
  const t = tracked.get(mint);
  if (!t) return;
  const tr = decodeTrade(buf);
  if (!t.creator) t.creator = tr.creator;
  if (!t.firstTradeTs) t.firstTradeTs = tr.ts;
  if (t.creationSlot == null && slot != null) t.creationSlot = slot;
  t.lastTradeTs = tr.ts;
  t.lastRealSol = tr.realSol;
  if (tr.realSol > t.peakRealSol) t.peakRealSol = tr.realSol;

  // --- détection de bundle : acheteurs distincts dans le bloc de création (+1) ---
  if (!t.bundleDone && tr.isBuy && slot != null && t.creationSlot != null) {
    if (slot - t.creationSlot <= BUNDLE_SLOT_WINDOW) {
      let set = t.slotBuyers.get(slot); if (!set) { set = new Set(); t.slotBuyers.set(slot, set); }
      set.add(tr.user); t.bundleSol += tr.solAmount;
      const early = new Set(); for (const s of t.slotBuyers.values()) for (const u of s) early.add(u);
      t.bundleSize = early.size;
      if (t.bundleSize >= BUNDLE_MIN && !t.bundled) { t.bundled = true; life.bundled++; }
    } else { t.bundleDone = true; t.slotBuyers = new Map(); } // fenêtre passée : on fige et on libère
  }

  if (tr.isBuy) { t.buys++; t.solIn += tr.solAmount; if (t.firstTradeTs && (tr.ts - t.firstTradeTs) <= SNIPER_WINDOW_S) t.snipers.add(tr.user); }
  else { t.sells++; t.solOut += tr.solAmount; if (tr.user === t.creator) t.devSoldSol += tr.solAmount; }
  if (tr.realSol >= 82) t.graduated = true;
  classifyOnce(t);
}

// ---- alertes survivants (vérifiées au rythme du rendu) ----
function checkSurvivors() {
  for (const t of tracked.values()) {
    if (t.survivorAlerted) continue;
    const [, label] = statusOf(t);
    if (label === "SURVIVANT") {
      t.survivorAlerted = true; life.survivors++;
      const msg = `$${t.symbol} tient ${Math.floor((nowSec() - t.bornSec) / 60)}min · réserve ${t.lastRealSol.toFixed(1)}◎ (pic ${t.peakRealSol.toFixed(1)}) · B/S ${t.buys}/${t.sells}${t.bundled ? " · BUNDLE" : ""} · ${t.mint}`;
      if (!QUIET) process.stdout.write(`\x07\x1b[36m⭐ SURVIVANT ${msg}\x1b[0m\n`);
      try { fs.appendFileSync(SURVIVORS, JSON.stringify({ t: now(), mint: t.mint, symbol: t.symbol, ageS: nowSec() - t.bornSec, peakRealSol: +t.peakRealSol.toFixed(2), lastRealSol: +t.lastRealSol.toFixed(2), buys: t.buys, sells: t.sells, bundled: t.bundled }) + "\n"); } catch {}
      notify("⭐ Token survivant pump.fun", msg);
    }
  }
}
const QUIET = process.argv.includes("--quiet");

// ---- dashboard ----
function render() {
  for (const [mint, t] of tracked) { const ref = t.lastTradeTs || t.bornSec; if (nowSec() - ref > EVICT_AFTER_S) tracked.delete(mint); }
  checkSurvivors();
  if (QUIET) return;

  const rows = [...tracked.values()].map(t => ({ t, sc: rugScore(t), st: statusOf(t) }));
  rows.sort((a, b) => { const rank = (s) => s === "SURVIVANT" ? 1000 : s === "RUG" ? 500 : s === "SUSPECT" ? 300 : 100; return (rank(b.st[1]) + b.sc) - (rank(a.st[1]) + a.sc) || ((b.t.lastTradeTs || 0) - (a.t.lastTradeTs || 0)); });

  const L = ["\x1b[2J\x1b[H"];
  L.push("\x1b[1m╔════════════════════════════════════════════════════════════════════════════╗");
  L.push("║   🛰️  MULTI-WATCH pump.fun — rug score + bundles + survivants (R/O, RPC)     ║");
  L.push("╚════════════════════════════════════════════════════════════════════════════╝\x1b[0m");
  L.push(`  suivis ${tracked.size} · nés ${life.born} · \x1b[31mrug ${life.rug}\x1b[0m ⚰️ ${life.dead} 🎓 ${life.graduated} · \x1b[35mbundle ${life.bundled}\x1b[0m · \x1b[36m⭐ ${life.survivors}\x1b[0m · SOL ${solUsd ? "$" + solUsd.toFixed(0) : "?"} · ${new Date().toISOString().slice(11, 19)}`);
  if (life.born) L.push(`  \x1b[2mmortalité ${((life.rug + life.dead) / life.born * 100).toFixed(0)}% · graduations ${(life.graduated / life.born * 100).toFixed(1)}% · bundles ${(life.bundled / life.born * 100).toFixed(0)}%${NTFY_TOPIC ? " · ntfy:" + NTFY_TOPIC : ""}\x1b[0m`);
  L.push("");
  L.push("  \x1b[2m  ÉTAT       $SYMBOL        âge   réserve(pic)     B/S     snip  flags      SCORE\x1b[0m");
  for (const { t, sc, st } of rows.slice(0, MAX_ROWS)) {
    const age = `${Math.floor((nowSec() - t.bornSec) / 60)}m${String((nowSec() - t.bornSec) % 60).padStart(2, "0")}`;
    const res = `${t.lastRealSol.toFixed(1)}(${t.peakRealSol.toFixed(1)})◎`;
    const flags = [t.devSoldSol > 0 ? "DUMP" : "", t.bundled ? "BND" + t.bundleSize : ""].filter(Boolean).join(" ") || " - ";
    const col = sc >= 60 ? "\x1b[31m" : sc >= 40 ? "\x1b[33m" : "\x1b[32m";
    const stCol = st[1] === "SURVIVANT" ? "\x1b[36m" : st[1] === "RUG" ? "\x1b[31m" : "";
    L.push(`  ${st[0]} ${stCol}${st[1].padEnd(9)}\x1b[0m ${("$" + t.symbol).slice(0, 13).padEnd(13)} ${age.padStart(5)}  ${res.padStart(13)}  ${(t.buys + "/" + t.sells).padStart(6)}  ${String(t.snipers.size).padStart(4)}  ${flags.slice(0, 9).padEnd(9)}  ${col}${String(sc).padStart(3)}\x1b[0m`);
  }
  if (!rows.length) L.push("  (en attente des naissances / trades…)");
  L.push("");
  L.push(`  \x1b[2mscore=fuite+dev-dump+ventes+snipers+bundle · logs/{token_outcomes,survivors}.jsonl · Ctrl-C\x1b[0m`);
  process.stdout.write(L.join("\n") + "\n");
}

// ---- connexions ----
function connectPump() {
  const pp = new WebSocket(PUMPPORTAL_WS);
  pp.addEventListener("open", () => pp.send(JSON.stringify({ method: "subscribeNewToken" })));
  pp.addEventListener("message", (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } if (m.txType !== "create" || !m.mint) return; if (!tracked.has(m.mint)) { tracked.set(m.mint, newToken(m.mint, m)); life.born++; } });
  pp.addEventListener("close", () => setTimeout(connectPump, 3000));
  pp.addEventListener("error", () => { try { pp.close(); } catch {} });
}
let rpc;
function connectRpc() {
  rpc = new WebSocket(RPC_WS);
  rpc.addEventListener("open", () => { rpc.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "logsSubscribe", params: [{ mentions: [PUMP_PROGRAM] }, { commitment: "processed" }] })); lastMsgAt = now(); });
  rpc.addEventListener("message", (ev) => {
    lastMsgAt = now();
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id === 1 && m.error) { console.log("[RPC erreur]", JSON.stringify(m.error)); return; }
    if (m.method !== "logsNotification") return;
    const slot = m.params?.result?.context?.slot;
    const val = m.params?.result?.value; if (val?.err) return;
    for (const line of (val?.logs || [])) {
      if (!line.startsWith("Program data: ")) continue;
      const buf = Buffer.from(line.slice(14).trim(), "base64");
      const mint = tradeMint(buf);
      if (mint && tracked.has(mint)) onTrade(mint, buf, slot);
    }
  });
  rpc.addEventListener("close", () => setTimeout(connectRpc, 3000));
  rpc.addEventListener("error", () => { try { rpc.close(); } catch {} });
}
setInterval(() => { if (now() - lastMsgAt > 8 * 60000) { try { rpc.close(); } catch {} } }, 60000);

(async () => {
  await refreshSol();
  console.log("Démarrage multi-watch (bundle + survivants)…");
  console.log(`RPC: ${RPC_WS.includes("helius") ? "Helius" : "public mainnet-beta"} · alerte: ${TG_TOKEN && TG_CHAT ? "Telegram" : NTFY_TOPIC ? "ntfy:" + NTFY_TOPIC : "off"}\n`);
  connectPump(); connectRpc();
  setInterval(refreshSol, 60000);
  setInterval(render, REFRESH_MS);
})();

process.on("SIGINT", () => { console.log(`\nArrêt. nés ${life.born} · rug ${life.rug} · morts ${life.dead} · gradués ${life.graduated} · bundles ${life.bundled} · survivants ${life.survivors}`); process.exit(0); });
