// paper_sniper.js — Bot sniper pump.fun en mode PAPIER (0 capital, 0 clé, 0 transaction réelle).
//
// Teste EMPIRIQUEMENT la stratégie "acheter vite après le dev". Pour chaque nouveau token :
//   1. réagit dès la notification PumpPortal (aussi vite qu'un bot maison peut)
//   2. MESURE le point d'entrée réel E = réserve déjà accumulée quand ton achat atterrirait
//      (notification + latence réaliste) → montre à quel point tu arrives DERRIÈRE les snipers
//   3. simule un achat de BUY_SOL au prix d'entrée, applique une règle de sortie (TP/SL/timeout)
//   4. tient le P&L papier, avec frais, et compare au "meilleur cas" (vente au pic)
//
//   node paper_sniper.js
//   LATENCY_MS=1500 BUY_SOL=0.05 TP=0.5 SL=0.4 TIMEOUT_S=120 node paper_sniper.js
//
// Prix bonding curve : price ∝ (30 + réserve_réelle)². P&L = ((30+sortie)/(30+entrée))² × frais − 1.

const fs = require("fs");
const path = require("path");
try { for (const l of fs.readFileSync(path.join(__dirname, ".env"), "utf8").split("\n")) { const m = l.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$/); if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, ""); } } catch {}

const PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const TRADE_DISC = Buffer.from([189, 219, 127, 211, 78, 230, 97, 238]);
const RPC_WS = process.env.RPC_WS || "wss://api.mainnet-beta.solana.com";
const PUMPPORTAL_WS = "wss://pumpportal.fun/api/data";

const LATENCY_MS = Number(process.env.LATENCY_MS || 1500);  // réaction + atterrissage de ta tx (réaliste maison)
const BUY_SOL = Number(process.env.BUY_SOL || 0.05);         // taille par snipe (~$3)
const TP = Number(process.env.TP || 0.5);                    // take-profit +50%
const SL = Number(process.env.SL || 0.4);                    // stop-loss -40%
const TIMEOUT_S = Number(process.env.TIMEOUT_S || 120);      // sortie forcée
const FEE = 0.99;                                            // ~1% par côté (frais pump.fun)
const TIP_SOL = Number(process.env.TIP_SOL || 0.001);       // frais de priorité de sortie PAR transaction
const JITO_MODE = process.env.JITO_MODE === "1";            // simule un atterrissage Jito parfait : fill à E≈0 (bloc de création, après le dev)
const JITO_TIP = Number(process.env.JITO_TIP || 0.005);     // pourboire Jito d'ENTRÉE pour gagner le slot du bundle (guerre de tips réaliste)
const FILL_WINDOW_MS = 20000;                                // si aucun acheteur après la latence, fill au niveau création
const REFRESH_MS = 4000;

const LOG_DIR = path.join(__dirname, "logs");
fs.mkdirSync(LOG_DIR, { recursive: true });
const TRADES_LOG = path.join(LOG_DIR, "paper_trades.jsonl");

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function bs58(b) { let n = 0n; for (const x of b) n = n * 256n + BigInt(x); let s = ""; while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; } for (const x of b) { if (x === 0) s = "1" + s; else break; } return s; }
function tradeMint(buf) { if (buf.length < 209 || !buf.subarray(0, 8).equals(TRADE_DISC)) return null; return bs58(buf.subarray(8, 40)); }
function realSolOf(buf) { return Number(buf.readBigUInt64LE(113)) / 1e9; }

const price = (R) => Math.pow(30 + R, 2); // ∝ prix
const positions = new Map(); // mint -> position
const closed = [];
const stats = { sniped: 0, filled: 0, closed: 0, wins: 0, pnlSol: 0, bestPnlSol: 0, entrySum: 0 };
let solUsd = null, lastMsgAt = Date.now();
const QUIET = process.argv.includes("--quiet");
const now = () => Date.now();

async function refreshSol() { try { const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", { signal: AbortSignal.timeout(8000) }); const j = await r.json(); if (j?.price) solUsd = parseFloat(j.price); } catch {} }
const usd = (sol) => solUsd ? (sol >= 0 ? "+" : "") + "$" + (sol * solUsd).toFixed(2) : (sol).toFixed(4) + "◎";

function onNewToken(m) {
  if (positions.has(m.mint)) return;
  stats.sniped++;
  const creationRes = Math.max(0, (m.vSolInBondingCurve || 30) - 30);
  const p = {
    mint: m.mint, symbol: m.symbol || "?", creator: m.traderPublicKey,
    t0: now(), creationRes,
    fillTarget: now() + LATENCY_MS,
    entryRes: null, fillTime: null, peakRes: creationRes, status: "waiting",
  };
  // mode Jito : tu es DANS le bloc de création → fill immédiat à E≈création (juste après le dev)
  if (JITO_MODE) { p.entryRes = creationRes; p.fillTime = now(); p.status = "open"; stats.filled++; stats.entrySum += creationRes; }
  positions.set(m.mint, p);
}

function onTrade(mint, R) {
  const p = positions.get(mint);
  if (!p) return;
  if (R > p.peakRes) p.peakRes = R;

  // FILL : 1er trade observé après (t0 + latence) → c'est là que ton achat atterrirait
  if (p.status === "waiting" && now() >= p.fillTarget) {
    p.entryRes = R; p.fillTime = now(); p.status = "open";
    stats.filled++; stats.entrySum += R;
    return;
  }
  if (p.status !== "open") return;

  // règle de sortie sur le prix courant
  const mult = (price(R) / price(p.entryRes)) * FEE * FEE;
  const ret = mult - 1;
  let reason = null;
  if (ret >= TP) reason = "TP";
  else if (ret <= -SL) reason = "SL";
  else if (now() - p.fillTime >= TIMEOUT_S * 1000) reason = "timeout";
  if (reason) closePosition(p, R, reason);
}

function closePosition(p, exitRes, reason) {
  p.status = "closed"; p.exitRes = exitRes; p.reason = reason;
  const tips = (JITO_MODE ? JITO_TIP : TIP_SOL) + TIP_SOL; // tip d'entrée (Jito en mode bundle) + tip de sortie
  const mult = (price(exitRes) / price(p.entryRes)) * FEE * FEE;
  p.pnlSol = BUY_SOL * (mult - 1) - tips;
  const bestMult = (price(p.peakRes) / price(p.entryRes)) * FEE * FEE; // si tu avais vendu au pic
  p.bestPnlSol = BUY_SOL * (bestMult - 1) - tips;
  stats.closed++; stats.pnlSol += p.pnlSol; stats.bestPnlSol += p.bestPnlSol;
  if (p.pnlSol > 0) stats.wins++;
  closed.unshift(p); if (closed.length > 8) closed.pop();
  try { fs.appendFileSync(TRADES_LOG, JSON.stringify({ t: now(), mint: p.mint, symbol: p.symbol, creationRes: +p.creationRes.toFixed(3), entryRes: +p.entryRes.toFixed(3), peakRes: +p.peakRes.toFixed(3), exitRes: +exitRes.toFixed(3), reason, pnlSol: +p.pnlSol.toFixed(5), bestPnlSol: +p.bestPnlSol.toFixed(5) }) + "\n"); } catch {}
  positions.delete(p.mint);
}

// expire les positions "waiting" sans acheteur (fill au niveau création puis token mort → sortie timeout)
function sweep() {
  for (const p of positions.values()) {
    if (p.status === "waiting" && now() - p.t0 > FILL_WINDOW_MS) { p.entryRes = p.creationRes; p.fillTime = now(); p.status = "open"; stats.filled++; stats.entrySum += p.entryRes; }
    if (p.status === "open" && now() - p.fillTime >= TIMEOUT_S * 1000) closePosition(p, p.peakRes || p.entryRes, "timeout-dead");
  }
}

function render() {
  sweep();
  if (QUIET) return;
  const avgE = stats.filled ? stats.entrySum / stats.filled : 0;
  const wr = stats.closed ? (stats.wins / stats.closed * 100) : 0;
  const L = ["\x1b[2J\x1b[H"];
  L.push("\x1b[1m╔══════════════════════════════════════════════════════════════════╗");
  L.push("║  🎯 PAPER SNIPER pump.fun — 'rapide après le dev' (0 capital)     ║");
  L.push("╚══════════════════════════════════════════════════════════════════╝\x1b[0m");
  L.push(`  latence simulée ${LATENCY_MS}ms · buy ${BUY_SOL}◎ · TP +${TP * 100}% · SL -${SL * 100}% · timeout ${TIMEOUT_S}s · SOL $${solUsd ? solUsd.toFixed(0) : "?"}`);
  L.push("");
  L.push(`  🎯 snipés ${stats.sniped} · remplis ${stats.filled} · clôturés ${stats.closed} · WR ${wr.toFixed(0)}%`);
  L.push(`  📍 entrée réelle MOYENNE : \x1b[1mE = ${avgE.toFixed(2)} SOL\x1b[0m  (à quel point tu arrives DERRIÈRE le dev/snipers)`);
  const col = stats.pnlSol >= 0 ? "\x1b[32m" : "\x1b[31m";
  L.push(`  💰 P&L PAPIER (règle TP/SL) : ${col}${usd(stats.pnlSol)}\x1b[0m  sur ${stats.closed} trades`);
  L.push(`  🌈 P&L si vente au PIC parfait : \x1b[36m${usd(stats.bestPnlSol)}\x1b[0m  (impossible — borne haute)`);
  if (stats.closed) L.push(`  \x1b[2mP&L moyen/trade : ${(stats.pnlSol / stats.closed * (solUsd || 0)).toFixed(3)}$ · investi simulé : $${(stats.closed * BUY_SOL * (solUsd || 0)).toFixed(0)}\x1b[0m`);
  L.push("");
  L.push("  \x1b[2mDerniers trades (entrée→sortie, raison, P&L) :\x1b[0m");
  for (const p of closed.slice(0, 6)) {
    const c = p.pnlSol >= 0 ? "\x1b[32m" : "\x1b[31m";
    L.push(`    $${(p.symbol || "?").slice(0, 10).padEnd(10)} E=${p.entryRes.toFixed(1)}◎→${p.exitRes.toFixed(1)}◎ pic ${p.peakRes.toFixed(1)} ${(p.reason || "").padEnd(11)} ${c}${usd(p.pnlSol)}\x1b[0m`);
  }
  L.push("");
  L.push(`  \x1b[2m${positions.size} positions ouvertes · logs/paper_trades.jsonl · Ctrl-C\x1b[0m`);
  process.stdout.write(L.join("\n") + "\n");
}

function connectPump() {
  const pp = new WebSocket(PUMPPORTAL_WS);
  pp.addEventListener("open", () => pp.send(JSON.stringify({ method: "subscribeNewToken" })));
  pp.addEventListener("message", (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } if (m.txType === "create" && m.mint) onNewToken(m); });
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
    if (m.method !== "logsNotification") return;
    const val = m.params?.result?.value; if (val?.err) return;
    for (const line of (val?.logs || [])) {
      if (!line.startsWith("Program data: ")) continue;
      const buf = Buffer.from(line.slice(14).trim(), "base64");
      const mint = tradeMint(buf);
      if (mint && positions.has(mint)) onTrade(mint, realSolOf(buf));
    }
  });
  rpc.addEventListener("close", () => setTimeout(connectRpc, 3000));
  rpc.addEventListener("error", () => { try { rpc.close(); } catch {} });
}
setInterval(() => { if (now() - lastMsgAt > 8 * 60000) { try { rpc.close(); } catch {} } }, 60000);

(async () => {
  await refreshSol();
  console.log(`Paper sniper démarré · ${JITO_MODE ? `MODE JITO (E≈0, tip entrée ${JITO_TIP}◎)` : `maison (latence ${LATENCY_MS}ms)`} · buy ${BUY_SOL}◎ · TP+${TP * 100}/SL-${SL * 100}/timeout ${TIMEOUT_S}s\n`);
  connectPump(); connectRpc();
  setInterval(refreshSol, 60000);
  setInterval(render, REFRESH_MS);
})();
process.on("SIGINT", () => { console.log(`\nArrêt. snipés ${stats.sniped} · clôturés ${stats.closed} · P&L papier ${usd(stats.pnlSol)} · entrée moy E=${(stats.filled ? stats.entrySum / stats.filled : 0).toFixed(2)} SOL`); process.exit(0); });
