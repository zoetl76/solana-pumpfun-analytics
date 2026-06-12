// decode_full.js — capture 1 TradeEvent réel et imprime un dump annoté complet octet-par-octet
// pour cartographier le layout 2026 (champs après virtualTokenReserves).
const PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const TRADE_DISC = Buffer.from([189, 219, 127, 211, 78, 230, 97, 238]);
const ws = new WebSocket("wss://api.mainnet-beta.solana.com");
const t = setTimeout(() => { console.log("timeout"); process.exit(0); }, 30000);

function bs58(buf) {
  const A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let n = 0n; for (const b of buf) n = n * 256n + BigInt(b);
  let s = ""; while (n > 0n) { s = A[Number(n % 58n)] + s; n /= 58n; }
  for (const b of buf) { if (b === 0) s = "1" + s; else break; }
  return s;
}

ws.addEventListener("open", () => ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "logsSubscribe", params: [{ mentions: [PUMP] }, { commitment: "confirmed" }] })));
ws.addEventListener("message", (ev) => {
  let m; try { m = JSON.parse(ev.data); } catch { return; }
  if (m.method !== "logsNotification") return;
  const val = m.params?.result?.value;
  if (val?.err) return;
  for (const line of (val?.logs || [])) {
    if (!line.startsWith("Program data: ")) continue;
    const buf = Buffer.from(line.slice(14).trim(), "base64");
    if (!buf.subarray(0, 8).equals(TRADE_DISC)) continue;
    clearTimeout(t);
    console.log("LEN =", buf.length, " sig =", val.signature, "\n");
    let o = 8;
    const u64 = (lbl) => { const v = buf.readBigUInt64LE(o); console.log(`@${String(o).padStart(3)} u64  ${lbl.padEnd(26)} = ${v}  (${Number(v) / 1e9} si SOL / ${Number(v) / 1e6} si token)`); o += 8; return v; };
    const i64 = (lbl) => { const v = buf.readBigInt64LE(o); console.log(`@${String(o).padStart(3)} i64  ${lbl.padEnd(26)} = ${v}`); o += 8; return v; };
    const pk = (lbl) => { const b = buf.subarray(o, o + 32); console.log(`@${String(o).padStart(3)} pk   ${lbl.padEnd(26)} = ${bs58(b)}`); o += 32; return b; };
    const u8 = (lbl) => { const v = buf.readUInt8(o); console.log(`@${String(o).padStart(3)} u8   ${lbl.padEnd(26)} = ${v}`); o += 1; return v; };
    const optPk = (lbl) => { const has = buf.readUInt8(o); o += 1; if (has) { const b = buf.subarray(o, o + 32); console.log(`@${String(o - 1).padStart(3)} opt? ${lbl.padEnd(26)} = Some(${bs58(b)})`); o += 32; } else console.log(`@${String(o - 1).padStart(3)} opt? ${lbl.padEnd(26)} = None`); };
    pk("mint"); u64("sol_amount"); u64("token_amount"); u8("is_buy"); pk("user"); i64("timestamp");
    u64("virtual_sol_reserves"); u64("virtual_token_reserves");
    console.log("  --- au-delà du cœur connu (offset", o, ", reste", buf.length - o, "octets) ---");
    u64("real_sol_reserves?"); u64("real_token_reserves?");
    pk("pubkey_A? (fee_recipient?)");
    u64("u64_a"); u64("u64_b");
    pk("pubkey_B? (creator?)");
    u64("u64_c"); u64("u64_d");
    // tente une string Borsh (len u32 + bytes)
    const slen = buf.readUInt32LE(o); console.log(`@${String(o).padStart(3)} u32  string_len               = ${slen}`); o += 4;
    if (slen > 0 && slen < 64 && o + slen <= buf.length) { console.log(`@${String(o).padStart(3)} str  string                   = "${buf.subarray(o, o + slen).toString("utf8")}"`); o += slen; }
    console.log("  --- reste après string (offset", o, ", reste", buf.length - o, "octets) ---");
    console.log("  hex reste:", buf.subarray(o).toString("hex"));
    ws.close(); process.exit(0);
  }
});
ws.addEventListener("error", (e) => console.log("[error]", e.message || e));
