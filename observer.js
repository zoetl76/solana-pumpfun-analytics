// observer.js — Observateur pump.fun en LECTURE SEULE (0 capital, 0 clé API).
//
// Se branche sur le WebSocket gratuit PumpPortal (wss://pumpportal.fun/api/data),
// s'abonne aux créations de tokens et aux migrations (= "graduations"), logge tout
// en JSONL et affiche un tableau de bord live qui rend visible la réalité de pump.fun :
//   - le débit de créations (le "firehose")
//   - combien gradue réellement (quasi rien)
//   - le spam de bots (mêmes noms relancés en boucle)
//   - les dev qui lancent en série (usines à rug)
//   - la taille des auto-achats des créateurs
//
// Aucune dépendance npm (WebSocket + fetch natifs Node 22+). Aucune transaction.
//   node observer.js                 # tableau de bord live
//   node observer.js --quiet         # logge sans réafficher (pour tourner en fond)

const fs = require("fs");
const path = require("path");

const WS_URL = "wss://pumpportal.fun/api/data";
const LOG_DIR = path.join(__dirname, "logs");
const EVENTS_LOG = path.join(LOG_DIR, "events.jsonl");
const QUIET = process.argv.includes("--quiet");
const REFRESH_MS = 15000;       // rafraîchit le tableau toutes les 15 s
const SOL_REFRESH_MS = 60000;   // prix SOL/USD toutes les 60 s

fs.mkdirSync(LOG_DIR, { recursive: true });

// ---- état ----
const state = {
  start: Date.now(),
  newCount: 0,
  migCount: 0,
  newTimes: [],                  // horodatages des créations (fenêtre glissante 60 s)
  migTimes: [],
  nameFreq: new Map(),           // nom -> nb de fois lancé (détecte le spam)
  symbolFreq: new Map(),
  devFreq: new Map(),            // wallet créateur -> nb de tokens créés (usines à rug)
  devBuySol: [],                 // tailles d'auto-achat des créateurs (SOL)
  recentTokens: [],              // 8 dernières créations
  recentMigs: [],                // 8 dernières graduations
  solUsd: null,
};

// ---- prix SOL/USD (Binance, lecture publique) ----
async function refreshSol() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", { signal: AbortSignal.timeout(8000) });
    const j = await r.json();
    if (j && j.price) state.solUsd = parseFloat(j.price);
  } catch { /* on garde l'ancien prix */ }
}

function fmtUsd(sol) {
  if (state.solUsd == null || sol == null) return `${sol?.toFixed?.(1) ?? "?"} SOL`;
  const usd = sol * state.solUsd;
  return usd >= 1000 ? `$${(usd / 1000).toFixed(1)}k` : `$${usd.toFixed(0)}`;
}

function topN(map, n) {
  return [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
}

function pruneWindow(arr, ms) {
  const cut = Date.now() - ms;
  while (arr.length && arr[0] < cut) arr.shift();
  return arr;
}

// ---- traitement d'un message ----
function onMessage(raw) {
  let m;
  try { m = JSON.parse(raw); } catch { return; }
  if (m.message) { if (!QUIET) console.log("·", m.message); return; }

  const now = Date.now();
  // journalise tout brut (avec notre horodatage de réception)
  try { fs.appendFileSync(EVENTS_LOG, JSON.stringify({ t: now, ...m }) + "\n"); } catch {}

  if (m.txType === "create") {
    state.newCount++;
    state.newTimes.push(now);
    if (m.name) state.nameFreq.set(m.name, (state.nameFreq.get(m.name) || 0) + 1);
    if (m.symbol) state.symbolFreq.set(m.symbol, (state.symbolFreq.get(m.symbol) || 0) + 1);
    if (m.traderPublicKey) state.devFreq.set(m.traderPublicKey, (state.devFreq.get(m.traderPublicKey) || 0) + 1);
    if (typeof m.solAmount === "number") state.devBuySol.push(m.solAmount);
    state.recentTokens.unshift({
      name: m.name || "?", symbol: m.symbol || "?",
      mint: m.mint, dev: m.traderPublicKey,
      buySol: m.solAmount, mcapSol: m.marketCapSol,
    });
    state.recentTokens = state.recentTokens.slice(0, 8);
  } else if (m.txType === "migrate") {
    state.migCount++;
    state.migTimes.push(now);
    state.recentMigs.unshift({ mint: m.mint, pool: m.pool });
    state.recentMigs = state.recentMigs.slice(0, 8);
  }
}

// ---- tableau de bord ----
function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function render() {
  if (QUIET) return;
  pruneWindow(state.newTimes, 60000);
  pruneWindow(state.migTimes, 60000);
  const upMin = (Date.now() - state.start) / 60000;
  const perMinNew = state.newTimes.length;            // créations sur les 60 dernières s
  const ratio = state.newCount ? (state.migCount / state.newCount * 100) : 0;
  const dupNames = [...state.nameFreq.values()].filter(v => v > 1).length;
  const serialDevs = [...state.devFreq.values()].filter(v => v > 1).length;
  const medBuy = median(state.devBuySol);

  const L = [];
  L.push("\x1b[2J\x1b[H"); // clear screen
  L.push("\x1b[1m╔══════════════════════════════════════════════════════════════╗\x1b[0m");
  L.push("\x1b[1m║   🔍 OBSERVATEUR pump.fun  —  LECTURE SEULE, 0 capital        ║\x1b[0m");
  L.push("\x1b[1m╚══════════════════════════════════════════════════════════════╝\x1b[0m");
  L.push(`  uptime ${upMin.toFixed(1)} min  ·  SOL ${state.solUsd ? "$" + state.solUsd.toFixed(0) : "?"}  ·  ${new Date().toISOString().slice(11, 19)} UTC`);
  L.push("");
  L.push(`  🆕 tokens créés      : \x1b[1m${state.newCount}\x1b[0m  (\x1b[33m${perMinNew}/min\x1b[0m en ce moment)`);
  L.push(`  🎓 graduations       : \x1b[1m${state.migCount}\x1b[0m`);
  L.push(`  📉 ratio gradué/créé : \x1b[1m${ratio.toFixed(2)}%\x1b[0m  ${state.migCount === 0 ? "(aucune graduation observée)" : ""}`);
  L.push("");
  L.push(`  🤖 noms dupliqués    : ${dupNames} noms relancés ≥2×  (spam de bots)`);
  L.push(`  🏭 dev en série      : ${serialDevs} wallets ont créé ≥2 tokens`);
  L.push(`  💸 auto-achat médian : ${medBuy != null ? medBuy.toFixed(3) + " SOL (" + fmtUsd(medBuy) + ")" : "?"}`);
  L.push("");

  const tn = topN(state.nameFreq, 5).filter(([, c]) => c > 1);
  if (tn.length) {
    L.push("  \x1b[2mNoms les plus spammés :\x1b[0m");
    for (const [name, c] of tn) L.push(`    ${c.toString().padStart(3)}×  ${name.slice(0, 40)}`);
    L.push("");
  }
  const td = topN(state.devFreq, 3).filter(([, c]) => c > 1);
  if (td.length) {
    L.push("  \x1b[2mDev les plus actifs (usines potentielles) :\x1b[0m");
    for (const [dev, c] of td) L.push(`    ${c.toString().padStart(3)} tokens  ${dev.slice(0, 6)}…${dev.slice(-4)}`);
    L.push("");
  }

  L.push("  \x1b[2mDernières créations :\x1b[0m");
  for (const t of state.recentTokens.slice(0, 6)) {
    L.push(`    ${(t.symbol || "?").slice(0, 10).padEnd(10)} ${(t.name || "").slice(0, 26).padEnd(26)} dev achète ${(t.buySol ?? 0).toFixed(3)} SOL`);
  }
  if (state.recentMigs.length) {
    L.push("");
    L.push("  \x1b[32m🎓 Dernières graduations :\x1b[0m");
    for (const g of state.recentMigs.slice(0, 4)) L.push(`    ${(g.mint || "?").slice(0, 6)}…${(g.mint || "").slice(-4)} → ${g.pool || "?"}`);
  }
  L.push("");
  L.push(`  \x1b[2mlog: logs/events.jsonl  ·  Ctrl-C pour arrêter\x1b[0m`);
  process.stdout.write(L.join("\n") + "\n");
}

// ---- connexion (avec reconnexion auto) ----
let ws;
function connect() {
  ws = new WebSocket(WS_URL);
  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ method: "subscribeNewToken" }));
    ws.send(JSON.stringify({ method: "subscribeMigration" }));
    if (!QUIET) console.log("[connecté] abonné à newToken + migration");
  });
  ws.addEventListener("message", (ev) => onMessage(ev.data));
  ws.addEventListener("close", () => {
    if (!QUIET) console.log("[déconnecté] reconnexion dans 3 s…");
    setTimeout(connect, 3000);
  });
  ws.addEventListener("error", () => { try { ws.close(); } catch {} });
}

refreshSol();
connect();
setInterval(refreshSol, SOL_REFRESH_MS);
setInterval(render, REFRESH_MS);
process.on("SIGINT", () => { console.log("\nArrêt. Total créés:", state.newCount, "| gradués:", state.migCount); process.exit(0); });
