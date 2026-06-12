// probe.js — Sonde jetable : se connecte au WS gratuit PumpPortal, capture quelques
// messages newToken + migration pour découvrir le schéma exact des champs. Lecture seule.
const URL = "wss://pumpportal.fun/api/data";
const ws = new WebSocket(URL);
let nNew = 0, nMig = 0;
const MAX_NEW = 3, MAX_MIG = 1;

const timer = setTimeout(() => { console.log("\n[timeout 30s] reçu", nNew, "newToken,", nMig, "migration"); ws.close(); process.exit(0); }, 30000);

ws.addEventListener("open", () => {
  console.log("[open] connecté", URL);
  ws.send(JSON.stringify({ method: "subscribeNewToken" }));
  ws.send(JSON.stringify({ method: "subscribeMigration" }));
  console.log("[sent] subscribeNewToken + subscribeMigration");
});

ws.addEventListener("message", (ev) => {
  let m;
  try { m = JSON.parse(ev.data); } catch { console.log("[raw non-json]", String(ev.data).slice(0, 200)); return; }
  // Premier message = souvent un accusé de réception
  if (m.message) { console.log("[ack]", JSON.stringify(m)); return; }
  const isMig = m.txType === "migrate" || m.pool === "pump-amm" || m.txType === "create" === false && m.signature && m.txType === undefined;
  if (m.txType === "create" && nNew < MAX_NEW) {
    nNew++;
    console.log(`\n=== NEW TOKEN #${nNew} (clés: ${Object.keys(m).join(", ")}) ===`);
    console.log(JSON.stringify(m, null, 2));
  } else if (m.txType === "migrate" && nMig < MAX_MIG) {
    nMig++;
    console.log(`\n=== MIGRATION #${nMig} (clés: ${Object.keys(m).join(", ")}) ===`);
    console.log(JSON.stringify(m, null, 2));
  } else if (nNew < MAX_NEW) {
    // schéma inconnu : on logge brut une fois
    console.log(`\n=== MSG (txType=${m.txType}, clés: ${Object.keys(m).join(", ")}) ===`);
    console.log(JSON.stringify(m, null, 2).slice(0, 800));
    nNew++;
  }
  if (nNew >= MAX_NEW && nMig >= MAX_MIG) { clearTimeout(timer); ws.close(); process.exit(0); }
});

ws.addEventListener("error", (e) => { console.log("[error]", e.message || e); });
ws.addEventListener("close", () => { console.log("[close]"); });
