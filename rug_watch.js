// rug_watch.js — Surveille la VIE d'UN token pump.fun en temps réel via RPC Solana GRATUIT.
// Lecture seule, 0 capital, 0 clé, 0 transaction.
//
// Décode chaque TradeEvent on-chain (logsSubscribe filtré sur le mint) et fait apparaître
// les patterns d'arnaque : DEV QUI DUMP, concentration whale, snipers de la première seconde,
// drainage de liquidité. Le but : VOIR un rug se produire, pour comprendre avant de risquer.
//
//   node rug_watch.js                 # attrape le prochain nouveau token et suit sa vie
//   node rug_watch.js <MINT>          # suit un token précis (son adresse de mint)
//
// Layout TradeEvent vérifié sur données réelles (pump.fun 2026), offsets fixes depuis le début.

const fs = require("fs");
const path = require("path");

const PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const TRADE_DISC = Buffer.from([189, 219, 127, 211, 78, 230, 97, 238]); // sha256("event:TradeEvent")[:8]
// RPC public par défaut (0 inscription) — OK pour observer. Pour du 24/7 fiable, mettre une
// clé Helius gratuite : RPC_WS="wss://mainnet.helius-rpc.com/?api-key=TA_CLE" node rug_watch.js
const RPC_WS = process.env.RPC_WS || "wss://api.mainnet-beta.solana.com";
const PUMPPORTAL_WS = "wss://pumpportal.fun/api/data";
const TOTAL_SUPPLY = 1_000_000_000; // tokens pump.fun standard
const REFRESH_MS = 4000;
const SNIPER_WINDOW_S = 15; // achats dans les N s après le 1er trade = snipers

const LOG_DIR = path.join(__dirname, "logs");
fs.mkdirSync(LOG_DIR, { recursive: true });

// ---------- base58 (pour afficher/compare les Pubkey) ----------
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function bs58(buf) {
  let n = 0n; for (const b of buf) n = n * 256n + BigInt(b);
  let s = ""; while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; }
  for (const b of buf) { if (b === 0) s = "1" + s; else break; }
  return s;
}
const shrt = (a) => a ? a.slice(0, 4) + "…" + a.slice(-4) : "?";

// ---------- décodage TradeEvent (offsets fixes confirmés en direct) ----------
function decodeTrade(buf) {
  if (buf.length < 209 || !buf.subarray(0, 8).equals(TRADE_DISC)) return null;
  return {
    mint: bs58(buf.subarray(8, 40)),
    solAmount: Number(buf.readBigUInt64LE(40)) / 1e9,
    tokenAmount: Number(buf.readBigUInt64LE(48)) / 1e6,
    isBuy: buf.readUInt8(56) === 1,
    user: bs58(buf.subarray(57, 89)),
    ts: Number(buf.readBigInt64LE(89)),
    vSol: Number(buf.readBigUInt64LE(97)) / 1e9,
    vTok: Number(buf.readBigUInt64LE(105)) / 1e6,
    realSol: Number(buf.readBigUInt64LE(113)) / 1e9,
    feeBps: Number(buf.readBigUInt64LE(161)),
    creator: bs58(buf.subarray(177, 209)), // champ creator embarqué dans chaque trade
  };
}

// ---------- état ----------
const S = {
  mint: process.argv[2] || null,
  creator: null,
  name: null, symbol: null,
  startSol: null,             // dev initial buy (depuis PumpPortal si lancé en direct)
  firstTradeTs: null,
  trades: 0, buys: 0, sells: 0,
  wallets: new Map(),         // wallet -> { tokens, solIn, solOut }
  snipers: new Set(),
  alerts: [],                 // {t, msg}
  lastVSol: null, lastVTok: null, lastRealSol: null,
  peakRealSol: 0,
  drainAlerted: new Set(),    // seuils de fuite de liquidité déjà signalés
  solUsd: null,
  watching: false,
  startedAt: Date.now(),
};

function wallet(w) {
  let o = S.wallets.get(w);
  if (!o) { o = { tokens: 0, solIn: 0, solOut: 0 }; S.wallets.set(w, o); }
  return o;
}
function alert(msg) {
  const a = { t: Date.now(), msg };
  S.alerts.unshift(a);
  S.alerts = S.alerts.slice(0, 12);
  if (!QUIET) process.stdout.write(`\x07\x1b[31m🚨 ${new Date().toISOString().slice(11, 19)} ${msg}\x1b[0m\n`);
  try { fs.appendFileSync(path.join(LOG_DIR, "rug_alerts.jsonl"), JSON.stringify(a) + "\n"); } catch {}
}
const QUIET = process.argv.includes("--quiet");

// ---------- prix SOL ----------
async function refreshSol() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", { signal: AbortSignal.timeout(8000) });
    const j = await r.json(); if (j?.price) S.solUsd = parseFloat(j.price);
  } catch {}
}
const usd = (sol) => S.solUsd && sol != null ? `$${(sol * S.solUsd).toLocaleString("en", { maximumFractionDigits: 0 })}` : `${(sol ?? 0).toFixed(2)} SOL`;

// ---------- traitement d'un trade ----------
function onTrade(tr) {
  if (S.mint && tr.mint !== S.mint) return;
  S.trades++;
  if (!S.firstTradeTs) S.firstTradeTs = tr.ts;
  if (!S.creator) S.creator = tr.creator;
  S.lastVSol = tr.vSol; S.lastVTok = tr.vTok; S.lastRealSol = tr.realSol;
  if (tr.realSol > S.peakRealSol) S.peakRealSol = tr.realSol;

  const w = wallet(tr.user);
  if (tr.isBuy) { S.buys++; w.tokens += tr.tokenAmount; w.solIn += tr.solAmount; }
  else { S.sells++; w.tokens -= tr.tokenAmount; w.solOut += tr.solAmount; }

  // sniper = achat dans la fenêtre initiale
  if (tr.isBuy && S.firstTradeTs && (tr.ts - S.firstTradeTs) <= SNIPER_WINDOW_S) S.snipers.add(tr.user);

  // ---- ALERTES ----
  // 1) DEV DUMP : le créateur vend
  if (!tr.isBuy && tr.user === tr.creator) {
    alert(`DEV DUMP — le créateur ${shrt(tr.creator)} VEND ${usd(tr.solAmount)} (real réserve ${tr.realSol.toFixed(1)} SOL)`);
  }
  // 2) gros sell d'un wallet qui détient une grosse part
  if (!tr.isBuy) {
    const drain = tr.solAmount;
    if (drain >= 1) alert(`GROS SELL — ${shrt(tr.user)} sort ${usd(drain)} d'un coup`);
  }
  // 3) LIQUIDITÉ EN FUITE : la réserve réelle chute fortement depuis son pic (le rug "soft")
  if (S.peakRealSol >= 2 && S.lastRealSol != null) {
    const dropPct = (1 - S.lastRealSol / S.peakRealSol) * 100;
    for (const th of [30, 50, 75, 90]) {
      if (dropPct >= th && !S.drainAlerted.has(th)) {
        S.drainAlerted.add(th);
        alert(`LIQUIDITÉ EN FUITE -${th}% — réserve ${S.peakRealSol.toFixed(1)} → ${S.lastRealSol.toFixed(1)} SOL`);
      }
    }
  }
  // 4) graduation imminente
  if (tr.realSol >= 80 && S.peakRealSol < 80) alert(`≈ GRADUATION proche — real réserve ${tr.realSol.toFixed(1)} SOL (~85 = migration PumpSwap)`);

  try { fs.appendFileSync(path.join(LOG_DIR, "rug_trades.jsonl"), JSON.stringify({ t: Date.now(), ...tr }) + "\n"); } catch {}
}

// ---------- dashboard ----------
function topHolders(n) {
  return [...S.wallets.entries()]
    .filter(([, o]) => o.tokens > 0)
    .sort((a, b) => b[1].tokens - a[1].tokens).slice(0, n);
}
function render() {
  if (QUIET || !S.watching) return;
  const age = S.firstTradeTs ? ((Date.now() / 1000) - S.firstTradeTs) : 0;
  const price = (S.lastVSol && S.lastVTok) ? S.lastVSol / S.lastVTok : 0; // SOL / token
  const mcapSol = price * TOTAL_SUPPLY;
  const holders = topHolders(5);
  const totHeld = [...S.wallets.values()].reduce((s, o) => s + Math.max(0, o.tokens), 0);
  const top1pct = holders.length ? (holders[0][1].tokens / (totHeld || 1) * 100) : 0;
  const devW = S.creator ? S.wallets.get(S.creator) : null;

  const L = ["\x1b[2J\x1b[H"];
  L.push("\x1b[1m╔════════════════════════════════════════════════════════════════╗");
  L.push("║   🩺 RUG WATCH — vie d'un token pump.fun en direct (RPC, R/O)   ║");
  L.push("╚════════════════════════════════════════════════════════════════╝\x1b[0m");
  L.push(`  ${S.symbol ? "$" + S.symbol + "  " : ""}${S.name || ""}`);
  L.push(`  mint    : ${S.mint}`);
  L.push(`  creator : ${S.creator || "?"} ${devW ? `(net ${devW.tokens > 0 ? "détient" : "a vendu"} ${Math.abs(devW.tokens).toLocaleString("en", { maximumFractionDigits: 0 })} tok)` : ""}`);
  L.push(`  âge ${age ? (age / 60).toFixed(1) + " min" : "?"}  ·  SOL ${S.solUsd ? "$" + S.solUsd.toFixed(0) : "?"}  ·  ${new Date().toISOString().slice(11, 19)} UTC`);
  L.push("");
  L.push(`  💧 réserve réelle : \x1b[1m${(S.lastRealSol ?? 0).toFixed(2)} SOL\x1b[0m (pic ${S.peakRealSol.toFixed(1)} · graduation ≈85)  mcap ≈ ${usd(mcapSol)}`);
  L.push(`  🔁 trades         : ${S.trades}  (\x1b[32m${S.buys} achats\x1b[0m / \x1b[31m${S.sells} ventes\x1b[0m)`);
  L.push(`  👛 wallets uniques: ${S.wallets.size}  ·  🔫 snipers (<${SNIPER_WINDOW_S}s): ${S.snipers.size}`);
  L.push(`  🐋 top holder     : ${top1pct.toFixed(1)}% (net depuis l'attache)  ${top1pct > 30 ? "\x1b[31m(CONCENTRÉ)\x1b[0m" : ""}`);
  L.push("");
  L.push("  \x1b[2mTop détenteurs :\x1b[0m");
  for (const [w, o] of holders) {
    const isDev = w === S.creator;
    L.push(`    ${isDev ? "👑" : "  "} ${shrt(w)}  ${o.tokens.toLocaleString("en", { maximumFractionDigits: 0 }).padStart(14)} tok   (in ${o.solIn.toFixed(2)} / out ${o.solOut.toFixed(2)} SOL)`);
  }
  L.push("");
  L.push("  \x1b[31m🚨 Alertes :\x1b[0m");
  if (!S.alerts.length) L.push("    (aucune pour l'instant)");
  for (const a of S.alerts.slice(0, 6)) L.push(`    ${new Date(a.t).toISOString().slice(11, 19)} ${a.msg}`);
  L.push("");
  L.push("  \x1b[2mlogs/rug_trades.jsonl · logs/rug_alerts.jsonl · Ctrl-C pour arrêter\x1b[0m");
  process.stdout.write(L.join("\n") + "\n");
}

// ---------- connexion RPC : écoute les trades du mint ----------
let rpc, lastMsgAt = Date.now();
function connectRpc() {
  rpc = new WebSocket(RPC_WS);
  rpc.addEventListener("open", () => {
    rpc.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "logsSubscribe", params: [{ mentions: [S.mint] }, { commitment: "processed" }] }));
    S.watching = true; lastMsgAt = Date.now();
    if (!QUIET) console.log(`[RPC] abonné aux trades de ${S.mint}`);
  });
  rpc.addEventListener("message", (ev) => {
    lastMsgAt = Date.now();
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id === 1 && m.error) { console.log("[RPC erreur souscription]", JSON.stringify(m.error)); return; }
    if (m.method !== "logsNotification") return;
    const val = m.params?.result?.value;
    if (val?.err) return;
    for (const line of (val?.logs || [])) {
      if (!line.startsWith("Program data: ")) continue;
      const tr = decodeTrade(Buffer.from(line.slice(14).trim(), "base64"));
      if (tr) onTrade(tr);
    }
  });
  rpc.addEventListener("close", () => { S.watching = false; setTimeout(connectRpc, 3000); });
  rpc.addEventListener("error", () => { try { rpc.close(); } catch {} });
}
// watchdog : le WS public coupe après ~10 min d'inactivité (token calme) → on reconnecte avant.
setInterval(() => { if (Date.now() - lastMsgAt > 8 * 60000) { try { rpc.close(); } catch {} } }, 60000);

// ---------- si pas de mint : attraper le prochain nouveau token via PumpPortal ----------
function grabFreshToken() {
  return new Promise((resolve) => {
    const pp = new WebSocket(PUMPPORTAL_WS);
    pp.addEventListener("open", () => pp.send(JSON.stringify({ method: "subscribeNewToken" })));
    pp.addEventListener("message", (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.txType !== "create") return;
      S.mint = m.mint; S.creator = m.traderPublicKey; S.name = m.name; S.symbol = m.symbol; S.startSol = m.solAmount;
      // crédite l'auto-achat initial du dev
      if (m.traderPublicKey && m.initialBuy) { const w = wallet(m.traderPublicKey); w.tokens += m.initialBuy; w.solIn += (m.solAmount || 0); }
      if (!QUIET) console.log(`[PumpPortal] nouveau token: $${m.symbol} "${m.name}" — dev ${shrt(m.traderPublicKey)} auto-achète ${(m.solAmount || 0).toFixed(3)} SOL`);
      pp.close(); resolve();
    });
    pp.addEventListener("error", () => {});
  });
}

(async () => {
  await refreshSol();
  if (!S.mint) { console.log("Aucun mint fourni — j'attrape le prochain token créé…"); await grabFreshToken(); }
  console.log(`Surveillance: ${S.mint}\n`);
  connectRpc();
  setInterval(refreshSol, 60000);
  setInterval(render, REFRESH_MS);
})();

process.on("SIGINT", () => {
  console.log(`\nArrêt. ${S.trades} trades observés sur ${S.mint} (${S.buys} achats / ${S.sells} ventes, ${S.alerts.length} alertes).`);
  process.exit(0);
});
