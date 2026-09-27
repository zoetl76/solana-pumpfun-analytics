"""Backtest M1 dédié à la stratégie « NY Open 9:31 » (or, XAUUSD).

Le backtester générique (``mt5.engine.backtest``) travaille sur des signaux à la
clôture de bougie ; cette stratégie repose sur des ORDRES STOP en attente, une
annulation croisée et une limite de temps, d'où ce simulateur spécifique.

Hypothèses de simulation (bougies M1 côté bid, spread de la bougie en points) :
- buy stop déclenché quand le ask (high + spread) atteint le prix d'ordre ;
  sell stop déclenché quand le bid (low) atteint le prix d'ordre ;
- entrée au prix de l'ordre (+ ``--slippage`` défavorable) ;
- achat : SL touché si low <= SL, TP si high >= TP ; vente : SL si high + spread >= SL,
  TP si low + spread <= TP ; SL et TP dans la même bougie → SL (prudent) ;
- gap à travers un niveau sur une bougie ultérieure : sortie au prix d'ouverture ;
- limite de temps : sortie à l'ouverture de la bougie ``max_hold_min`` minutes après
  la bougie de déclenchement ;
- niveaux touchés dans la même bougie de déclenchement → ambigu, direction du corps ;
- risque « second ordre » : si le niveau opposé est touché avant l'annulation OCO
  (latence ``--oco-latency-sec``), la seconde position est fermée au marché et coûte
  spread (+ 2 × slippage) × lots × contrat ; ce coût est inclus dans le PnL du jour.

Exemple :
  python3 -m mt5.engine.backtest_nyopen --candles "mt5/data/nyopen/*.json" --balance 10000
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from mt5.engine.backtest import french_parser
from mt5.engine.candles import Candle, load_candles
from mt5.engine.strategy import SymbolSpec, parse_params_string
from mt5.strategies import ny_open_931 as ny


# --------------------------------------------------------------------------- #
# Résultats
# --------------------------------------------------------------------------- #
@dataclass
class DayResult:
    day: str
    status: str  # "trade" | "aucun" | "saute"
    reason: str = ""
    high: float | None = None
    low: float | None = None
    range: float | None = None
    spread: float | None = None
    side: str | None = None
    ambiguous: bool = False
    trigger_time: str | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    lots: float | None = None
    risk_money: float | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float = 0.0
    r: float = 0.0
    reversal_hit: bool = False
    reversal_cost: float = 0.0
    equity_after: float | None = None


@dataclass
class Summary:
    days: int = 0
    days_with_ref_bar: int = 0
    trades: int = 0
    no_trigger: int = 0
    skipped: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    tp_exits: int = 0
    sl_exits: int = 0
    time_exits: int = 0
    other_exits: int = 0
    ambiguous: int = 0
    reversal_events: int = 0
    reversal_cost: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    total_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    avg_r: float = 0.0
    expectancy: float = 0.0
    max_drawdown_abs: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    final_equity: float = 0.0
    start_balance: float = 0.0
    range_median: float | None = None
    range_min: float | None = None
    range_max: float | None = None
    by_side: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_month: dict[str, dict[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Simulation d'une journée
# --------------------------------------------------------------------------- #
def _spread_of(bar: Candle, spec: SymbolSpec, extra: float) -> float:
    return bar.spread_points * spec.point + extra


def simulate_day(
    day: date,
    bars: list[Candle],
    equity: float,
    spec: SymbolSpec,
    cfg: ny.NyOpenConfig,
    spread_extra: float = 0.0,
    slippage: float = 0.0,
    oco_latency_sec: int = 60,
) -> DayResult:
    """Rejoue la stratégie sur les bougies M1 d'un jour (date locale New York)."""
    bars = sorted(bars, key=lambda b: b.time)
    ref = ny.find_ref_bar(bars, day, cfg)
    if ref is None:
        return DayResult(day.isoformat(), "saute", reason="pas de bougie de référence 9h31")
    spread_ref = _spread_of(ref, spec, spread_extra)
    the_plan = ny.plan(ref, spread_ref, equity, spec, cfg)
    res = DayResult(
        day.isoformat(), "saute", high=the_plan.high, low=the_plan.low,
        range=round(the_plan.range, spec.digits), spread=round(spread_ref, spec.digits),
    )
    if the_plan.skipped:
        res.reason = the_plan.skipped
        return res
    buy, sell = the_plan.orders

    # 1) Déclenchement dans la fenêtre d'entrée.
    after = [b for b in bars if b.time > ref.time]
    trigger: Candle | None = None
    side: str | None = None
    ambiguous = False
    for b in after:
        if b.time >= the_plan.expiration:
            break
        sp = _spread_of(b, spec, spread_extra)
        buy_hit = b.high + sp >= buy.price - 1e-9
        sell_hit = b.low <= sell.price + 1e-9
        if buy_hit and sell_hit:
            ambiguous = True
            side = "buy" if b.close >= b.open else "sell"
        elif buy_hit:
            side = "buy"
        elif sell_hit:
            side = "sell"
        if side:
            trigger = b
            break
    if trigger is None or side is None:
        res.status = "aucun"
        res.reason = "aucun ordre déclenché dans la fenêtre d'entrée"
        res.equity_after = equity
        return res

    order = buy if side == "buy" else sell
    opposite = sell if side == "buy" else buy
    entry = order.price + slippage if side == "buy" else order.price - slippage
    lots = order.lots
    risk_money = lots * abs(entry - order.sl) * spec.contract_size
    res.status, res.side, res.ambiguous = "trade", side, ambiguous
    res.trigger_time = trigger.time.isoformat()
    res.entry, res.sl, res.tp, res.lots = entry, order.sl, order.tp, lots
    res.risk_money = round(risk_money, 2)

    # 2) Gestion jusqu'au SL / TP / limite de temps.
    time_stop = trigger.time + timedelta(minutes=cfg.max_hold_min)
    exit_price: float | None = None
    exit_reason = ""
    exit_time: datetime | None = None
    started = False
    for b in bars:
        if b.time < trigger.time:
            continue
        sp = _spread_of(b, spec, spread_extra)
        first = b.time == trigger.time
        if b.time >= time_stop:
            exit_price = b.open - slippage if side == "buy" else b.open + sp + slippage
            exit_reason, exit_time = "temps", b.time
            break
        if side == "buy":
            sl_hit, tp_hit = b.low <= order.sl + 1e-9, b.high >= order.tp - 1e-9
            if sl_hit:
                exit_price = order.sl if first else min(order.sl, b.open)
                exit_price -= slippage
                exit_reason = "SL"
            elif tp_hit:
                exit_price = order.tp if first else max(order.tp, b.open)
                exit_reason = "TP"
        else:
            sl_hit, tp_hit = b.high + sp >= order.sl - 1e-9, b.low + sp <= order.tp + 1e-9
            if sl_hit:
                exit_price = order.sl if first else max(order.sl, b.open + sp)
                exit_price += slippage
                exit_reason = "SL"
            elif tp_hit:
                exit_price = order.tp if first else min(order.tp, b.open + sp)
                exit_reason = "TP"
        started = True
        if exit_price is not None:
            exit_time = b.time
            break
    if exit_price is None:
        last = bars[-1]
        sp = _spread_of(last, spec, spread_extra)
        exit_price = last.close if side == "buy" else last.close + sp
        exit_reason, exit_time = "fin des données", last.time
    pnl = (exit_price - entry) * lots * spec.contract_size
    if side == "sell":
        pnl = -pnl

    # 3) Risque « second ordre » avant l'annulation OCO.
    latency_end = trigger.time + timedelta(seconds=oco_latency_sec + 60)
    for b in bars:
        if b.time < trigger.time or b.time >= latency_end:
            continue
        sp = _spread_of(b, spec, spread_extra)
        hit = (b.low <= opposite.price + 1e-9) if side == "buy" else (b.high + sp >= opposite.price - 1e-9)
        if hit:
            res.reversal_hit = True
            res.reversal_cost = round((sp + 2 * slippage) * opposite.lots * spec.contract_size, 2)
            break
    pnl -= res.reversal_cost

    res.exit_time = exit_time.isoformat() if exit_time else None
    res.exit_price = round(exit_price, spec.digits)
    res.exit_reason = exit_reason
    res.pnl = round(pnl, 2)
    res.r = round(pnl / risk_money, 3) if risk_money > 0 else 0.0
    res.equity_after = round(equity + pnl, 2)
    return res


# --------------------------------------------------------------------------- #
# Backtest complet
# --------------------------------------------------------------------------- #
def group_by_local_day(candles: list[Candle], cfg: ny.NyOpenConfig) -> dict[date, list[Candle]]:
    groups: dict[date, list[Candle]] = defaultdict(list)
    for c in candles:
        groups[ny.local_day(c.time, cfg)].append(c)
    return dict(sorted(groups.items()))


def run_backtest(
    candles: list[Candle],
    spec: SymbolSpec,
    cfg: ny.NyOpenConfig,
    balance: float,
    compound: bool = False,
    spread_extra: float = 0.0,
    slippage: float = 0.0,
    oco_latency_sec: int = 60,
    day_from: date | None = None,
    day_to: date | None = None,
) -> tuple[Summary, list[DayResult]]:
    equity = float(balance)
    results: list[DayResult] = []
    for day, bars in group_by_local_day(candles, cfg).items():
        if day_from and day < day_from:
            continue
        if day_to and day > day_to:
            continue
        sizing_equity = equity if compound else float(balance)
        res = simulate_day(day, bars, sizing_equity, spec, cfg, spread_extra, slippage, oco_latency_sec)
        if res.status == "trade":
            equity += res.pnl
        res.equity_after = round(equity, 2)
        results.append(res)
    return summarize(results, balance, equity), results


def summarize(results: list[DayResult], balance: float, final_equity: float) -> Summary:
    s = Summary(start_balance=float(balance), final_equity=round(final_equity, 2))
    peak = float(balance)
    equity = float(balance)
    streak = 0
    r_values: list[float] = []
    ranges: list[float] = []
    side_stats: dict[str, dict[str, Any]] = {}
    month_stats: dict[str, dict[str, Any]] = {}
    for res in results:
        s.days += 1
        if res.range is not None:
            s.days_with_ref_bar += 1
            ranges.append(res.range)
        if res.status == "saute":
            s.skipped += 1
            continue
        if res.status == "aucun":
            s.no_trigger += 1
            continue
        s.trades += 1
        s.ambiguous += int(res.ambiguous)
        s.reversal_events += int(res.reversal_hit)
        s.reversal_cost += res.reversal_cost
        r_values.append(res.r)
        if res.pnl > 0:
            s.wins += 1
            s.gross_profit += res.pnl
            streak = 0
        elif res.pnl < 0:
            s.losses += 1
            s.gross_loss += res.pnl
            streak += 1
            s.max_consecutive_losses = max(s.max_consecutive_losses, streak)
        else:
            s.flats += 1
            streak = 0
        if res.exit_reason == "TP":
            s.tp_exits += 1
        elif res.exit_reason == "SL":
            s.sl_exits += 1
        elif res.exit_reason == "temps":
            s.time_exits += 1
        else:
            s.other_exits += 1
        equity += res.pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > s.max_drawdown_abs:
            s.max_drawdown_abs = dd
            s.max_drawdown_pct = dd / peak * 100 if peak > 0 else 0.0
        side = side_stats.setdefault(res.side or "?", {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        side["trades"] += 1
        side["wins"] += int(res.pnl > 0)
        side["pnl"] = round(side["pnl"] + res.pnl, 2)
        side["r"] = round(side["r"] + res.r, 2)
        month = month_stats.setdefault(res.day[:7], {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        month["trades"] += 1
        month["wins"] += int(res.pnl > 0)
        month["pnl"] = round(month["pnl"] + res.pnl, 2)
        month["r"] = round(month["r"] + res.r, 2)
    s.net_pnl = round(s.gross_profit + s.gross_loss, 2)
    s.gross_profit = round(s.gross_profit, 2)
    s.gross_loss = round(s.gross_loss, 2)
    s.reversal_cost = round(s.reversal_cost, 2)
    s.total_r = round(sum(r_values), 2)
    s.win_rate = round(s.wins / s.trades * 100, 1) if s.trades else 0.0
    s.profit_factor = round(s.gross_profit / abs(s.gross_loss), 2) if s.gross_loss < 0 else None
    s.avg_r = round(s.total_r / s.trades, 3) if s.trades else 0.0
    s.expectancy = round(s.net_pnl / s.trades, 2) if s.trades else 0.0
    s.max_drawdown_abs = round(s.max_drawdown_abs, 2)
    s.max_drawdown_pct = round(s.max_drawdown_pct, 2)
    if ranges:
        s.range_median = round(statistics.median(ranges), 2)
        s.range_min, s.range_max = min(ranges), max(ranges)
    s.by_side = side_stats
    s.by_month = month_stats
    return s


# --------------------------------------------------------------------------- #
# Affichage
# --------------------------------------------------------------------------- #
def format_report(s: Summary, results: list[DayResult], cfg: ny.NyOpenConfig, show_days: bool) -> str:
    pf = "∞" if s.profit_factor is None and s.gross_profit > 0 else (f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a")
    lines = [
        "=" * 64,
        " BACKTEST « NY Open 9:31 » — " + cfg.symbol,
        "=" * 64,
        f" Paramètres          : buffer achat {cfg.buffer_buy} (+spread={cfg.add_spread_to_buy}), buffer vente {cfg.buffer_sell}, RR {cfg.rr}, risque {cfg.risk_pct} %",
        f" Fenêtre / durée max : {cfg.entry_window_min} min / {cfg.max_hold_min} min",
        f" Jours analysés      : {s.days} (bougie 9h31 présente : {s.days_with_ref_bar}, sautés : {s.skipped}, sans déclenchement : {s.no_trigger})",
        f" Amplitude 9h31      : min {s.range_min} / médiane {s.range_median} / max {s.range_max} USD",
        f" Trades              : {s.trades} (achats {s.by_side.get('buy', {}).get('trades', 0)}, ventes {s.by_side.get('sell', {}).get('trades', 0)}, ambigus {s.ambiguous})",
        f" Sorties             : TP {s.tp_exits} / SL {s.sl_exits} / temps {s.time_exits} / autres {s.other_exits}",
        f" Gagnants / Perdants : {s.wins} / {s.losses} (nuls {s.flats}) — taux {s.win_rate} %",
        f" Total R             : {s.total_r:+.2f} R (moyenne {s.avg_r:+.3f} R par trade)",
        f" PnL net             : {s.net_pnl:+,.2f} USD (brut +{s.gross_profit:,.2f} / {s.gross_loss:,.2f}) — espérance {s.expectancy:+.2f} USD/trade",
        f" Facteur de profit   : {pf}",
        f" Drawdown max        : {s.max_drawdown_abs:,.2f} USD ({s.max_drawdown_pct:.2f} %) — pertes consécutives max {s.max_consecutive_losses}",
        f" Second ordre (OCO)  : {s.reversal_events} déclenchement(s) du niveau opposé, coût inclus {s.reversal_cost:,.2f} USD",
        f" Solde               : {s.start_balance:,.2f} → {s.final_equity:,.2f} USD",
        "-" * 64,
        " Par mois            : " + ", ".join(f"{m} {v['trades']}t {v['r']:+.1f}R {v['pnl']:+.0f}$" for m, v in s.by_month.items()),
        " Par sens            : " + ", ".join(f"{k} {v['trades']}t gagn. {v['wins']} {v['r']:+.1f}R" for k, v in s.by_side.items()),
        "=" * 64,
    ]
    if show_days:
        lines.append(" Jour       | Ampl. | Sens | Déclench. | Sortie          | R      | PnL")
        for r in results:
            if r.status == "trade":
                lines.append(
                    f" {r.day} | {r.range:5.2f} | {r.side:4s} | {r.trigger_time[11:16] if r.trigger_time else '     '}     | "
                    f"{(r.exit_reason or ''):5s} {r.exit_time[11:16] if r.exit_time else '     '}     | {r.r:+6.2f} | {r.pnl:+8.2f}"
                    + (" (ambigu)" if r.ambiguous else "") + (" (2e ordre)" if r.reversal_hit else "")
                )
            else:
                lines.append(f" {r.day} | {r.range if r.range is not None else '  -  '} | {r.status:5s} {r.reason}")
    return "\n".join(lines)


def load_many(paths: list[str]) -> list[Candle]:
    files: list[str] = []
    for p in paths:
        matched = sorted(glob.glob(p))
        files.extend(matched if matched else [p])
    seen: dict[datetime, Candle] = {}
    for f in files:
        for c in load_candles(f):
            seen[c.time] = c
    return [seen[t] for t in sorted(seen)]


def build_parser() -> argparse.ArgumentParser:
    parser = french_parser(
        "python3 -m mt5.engine.backtest_nyopen",
        "Backtest M1 de la stratégie « NY Open 9:31 » (ordres stop, OCO, limite de temps).",
        "Les bougies sont au format du connecteur Axi (get_mt5_candles, période M1).",
    )
    parser.add_argument("--candles", nargs="+", required=True, help="fichiers ou motifs glob de bougies M1")
    parser.add_argument("--spec", help="symbol.json ; défaut : XAUUSD Axi (2 décimales, contrat 100 oz)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="solde de départ (défaut 10000)")
    parser.add_argument("--compound", action="store_true", help="taille calculée sur l'équité courante (défaut : solde initial fixe)")
    parser.add_argument("--params", help="paramètres de stratégie clé=valeur (ex. buffer_buy=0.1,rr=2,risk_pct=0.5)")
    parser.add_argument("--spread-extra", type=float, default=0.0, help="spread ajouté à celui des bougies, en USD (défaut 0)")
    parser.add_argument("--slippage", type=float, default=0.0, help="glissement défavorable par exécution au marché, en USD (défaut 0)")
    parser.add_argument("--oco-latency-sec", type=int, default=60, help="latence d'annulation de l'ordre opposé (défaut 60 s)")
    parser.add_argument("--from", dest="day_from", help="première date locale AAAA-MM-JJ")
    parser.add_argument("--to", dest="day_to", help="dernière date locale AAAA-MM-JJ")
    parser.add_argument("--days", action="store_true", help="afficher le détail jour par jour")
    parser.add_argument("--json", help="écrire résumé et détail dans ce fichier JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = ny.NyOpenConfig.from_params(parse_params_string(args.params))
    if args.spec:
        with open(args.spec, encoding="utf-8") as handle:
            spec = SymbolSpec.from_dict(json.load(handle), cfg.symbol)
    else:
        from mt5.engine.nyopen_cycle import _default_gold_spec
        spec = _default_gold_spec(cfg.symbol)
    candles = load_many(args.candles)
    if not candles:
        print("erreur : aucune bougie chargée", file=sys.stderr)
        return 2
    summary, results = run_backtest(
        candles, spec, cfg, args.balance, compound=args.compound, spread_extra=args.spread_extra,
        slippage=args.slippage, oco_latency_sec=args.oco_latency_sec,
        day_from=date.fromisoformat(args.day_from) if args.day_from else None,
        day_to=date.fromisoformat(args.day_to) if args.day_to else None,
    )
    print(format_report(summary, results, cfg, args.days))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({
            "strategy": ny.NAME, "params": cfg.to_dict(), "summary": asdict(summary),
            "days": [asdict(r) for r in results],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
