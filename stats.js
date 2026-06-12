// stats.js — Analyse le dataset accumulé par multi_watch.js (lecture seule).
//   node stats.js
// Lit logs/token_outcomes.jsonl (sorts terminaux) + logs/survivors.jsonl et sort des stats robustes.

const fs = require("fs");
const path = require("path");
const LOG = path.join(__dirname, "logs");

function load(f) { try { return fs.readFileSync(path.join(LOG, f), "utf8").trim().split("\n").filter(Boolean).map(JSON.parse); } catch { return []; } }
function pct(arr, p) { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(p / 100 * s.length))]; }
function fmtDur(ms) { const m = Math.floor(ms / 60000); const h = Math.floor(m / 60); return h ? `${h}h${String(m % 60).padStart(2, "0")}` : `${m}min`; }

const O = load("token_outcomes.jsonl");
const SUR = load("survivors.jsonl");

if (!O.length) { console.log("Aucun sort enregistré dans logs/token_outcomes.jsonl — laisse multi_watch.js tourner d'abord."); process.exit(0); }

const span = O.length > 1 ? O[O.length - 1].t - O[0].t : 0;
const by = (lbl) => O.filter(x => x.outcome === lbl);
const rug = by("RUG"), mort = by("MORT"), mortne = by("MORT-NÉ"), grad = by("GRADUÉ");
const dead = mort.length + mortne.length;
const bundled = O.filter(x => x.bundled);
const withDump = O.filter(x => x.devSoldSol > 0);
const n = O.length;
const p = (x) => (x / n * 100).toFixed(1) + "%";

console.log("══════════════════════════════════════════════════════════════");
console.log("  STATS pump.fun — dataset multi_watch  (lecture seule)");
console.log("══════════════════════════════════════════════════════════════");
console.log(`  fenêtre couverte    : ${fmtDur(span)}  (${n} tokens classés)`);
console.log("");
console.log("  SORTS");
console.log(`    🔴 RUG            : ${rug.length.toString().padStart(5)}  ${p(rug.length)}`);
console.log(`    ⚰️  morts/mort-nés : ${dead.toString().padStart(5)}  ${p(dead)}`);
console.log(`    🎓 GRADUÉ         : ${grad.length.toString().padStart(5)}  ${p(grad.length)}   <= les rares qui atteignent PumpSwap`);
console.log(`    => mortalité totale: ${p(rug.length + dead)}`);
console.log("");
console.log("  PATTERNS D'ARNAQUE");
console.log(`    dev-dump (créateur vend) : ${withDump.length}/${n}  ${p(withDump.length)}`);
console.log(`    lancements en BUNDLE     : ${bundled.length}/${n}  ${p(bundled.length)}  (créateur + ≥4 acheteurs même bloc)`);
if (bundled.length) {
  const bRug = bundled.filter(x => x.outcome === "RUG").length;
  const nbRug = O.filter(x => !x.bundled && x.outcome === "RUG").length;
  const nb = n - bundled.length;
  console.log(`      → taux de rug si bundle  : ${(bRug / bundled.length * 100).toFixed(0)}%   vs   sans bundle : ${nb ? (nbRug / nb * 100).toFixed(0) : "?"}%`);
}
console.log("");
console.log("  TEMPS DE SURVIE (avant rug)");
const ages = rug.map(x => x.ageS);
if (ages.length) console.log(`    âge au rug  médian ${pct(ages, 50)}s · p25 ${pct(ages, 25)}s · p75 ${pct(ages, 75)}s · max ${Math.max(...ages)}s`);
console.log("");
console.log("  LIQUIDITÉ ATTEINTE (réserve réelle pic, SOL — graduation à ~85)");
const peaks = O.map(x => x.peakRealSol);
console.log(`    médian ${pct(peaks, 50)}◎ · p90 ${pct(peaks, 90)}◎ · max ${Math.max(...peaks).toFixed(1)}◎`);
console.log(`    >20◎ : ${O.filter(x => x.peakRealSol > 20).length} tokens (${p(O.filter(x => x.peakRealSol > 20).length)}) · >50◎ : ${O.filter(x => x.peakRealSol > 50).length}`);
console.log("");
const minAge = SUR.length ? Math.min(...SUR.map(s => s.ageS)) : 0;
console.log(`  ⭐ SURVIVANTS (>~${Math.round(minAge / 60)} min, sains) : ${SUR.length}  (${(SUR.length / n * 100).toFixed(1)}% des tokens)`);
for (const s of SUR.slice(-8)) console.log(`    $${(s.symbol || "?").padEnd(12)} ${Math.floor(s.ageS / 60)}min · pic ${s.peakRealSol}◎ · B/S ${s.buys}/${s.sells}${s.bundled ? " · BUNDLE" : ""}  ${s.mint}`);
console.log("");
console.log("  Rappel : graduer ≠ gagner. Même un token gradué peut s'effondrer ensuite sur PumpSwap.");
console.log("══════════════════════════════════════════════════════════════");
