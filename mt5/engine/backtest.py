"""Backtester bougie par bougie (sans dépendance).

Modèle d'exécution
------------------
- La stratégie décide à la CLÔTURE de la bougie ``i`` (``on_bar``) ; l'ordre
  est exécuté à l'OUVERTURE de la bougie ``i+1`` : achat au ask
  (``open + spread_points × point``), vente au bid (``open``).
- Au moment de l'exécution (ouverture de ``i+1``) on rejoue les contrôles du
  cycle live : spread de la bougie d'exécution (``max_spread``), validité des
  stops par rapport au bid/ask comme le ferait le serveur MT5
  (``invalid_stops``) et marge disponible (``margin``, réduction du volume ou
  refus). Un signal refusé est compté dans ``blocked``.
- SL/TP vérifiés en intrabar sur le high/low de chaque bougie, avec
  l'asymétrie bid/ask traitée simplement :
  * achat : SL touché si ``low <= sl`` ; TP touché si ``high >= tp`` ;
  * vente : SL touché si ``high + spread >= sl`` ; TP touché si ``low + spread <= tp``.
  Si SL ET TP sont touchés dans la même bougie, on retient le SL (prudent).
  Si la bougie OUVRE déjà au-delà du niveau (gap de week-end, annonce), la
  sortie est exécutée au prix d'ouverture, comme un ordre au marché, et non
  au niveau du stop (raison suffixée « (gap) »).
- Sortie discrétionnaire (``should_exit`` à la clôture) → exécutée à
  l'ouverture suivante. ``manage`` (trailing) appliqué à chaque clôture.
- Taille via ``risk.compute_lots`` puis ``risk.check_margin`` (même code que
  le live) sur l'équité courante.
- PnL (devise du compte) = (sortie − entrée) × lots × contractSize × taux,
  signe selon la direction.
- Drawdown : calculé sur l'équité valorisée à CHAQUE clôture de bougie
  (position ouverte évaluée au prix de clôture), pas seulement sur les trades
  clôturés. ``equity_curve`` ne contient en revanche que les points de
  clôture de trade (plus le point de départ).

Utilisation en ligne de commande ::

    python3 -m mt5.engine.backtest --strategy ema_cross \
        --candles mt5/data/EURUSD_H1_sample.json --symbol EURUSD --balance 10000 \
        [--risk-pct 1] [--params fast=9,slow=21] [--json out.json] [--spec symbol.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .candles import Candle, candle_close_time, infer_timeframe, load_candles
from .risk import (
    Blocked,
    RiskConfig,
    check_margin,
    compute_lots,
    guard_daily_loss_amount,
    guard_friday_cutoff,
    guard_spread,
    guard_trading_hours,
    round_lots_down,
    validate_stops,
)
from .strategy import AccountState, Context, ExitSignal, PositionState, Signal, Strategy, SymbolSpec, parse_params_string


# --------------------------------------------------------------------------- #
# Structures de résultat
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    """Un aller-retour terminé."""

    direction: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    lots: float
    pnl: float
    reason: str
    entry_reason: str = ""
    sl: float | None = None
    tp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entryTime": self.entry_time.isoformat(),
            "entryPrice": self.entry_price,
            "exitTime": self.exit_time.isoformat(),
            "exitPrice": self.exit_price,
            "lots": self.lots,
            "pnl": round(self.pnl, 2),
            "reason": self.reason,
            "entryReason": self.entry_reason,
            "sl": self.sl,
            "tp": self.tp,
        }


@dataclass
class _OpenTrade:
    direction: str
    entry_time: datetime
    entry_price: float
    lots: float
    sl: float | None
    tp: float | None
    entry_reason: str
    entry_index: int


@dataclass
class BacktestResult:
    """Statistiques et journal d'un backtest."""

    strategy: str
    symbol: str
    initial_balance: float
    final_equity: float
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_pnl: float
    profit_factor: float | None
    max_drawdown_abs: float
    max_drawdown_pct: float
    avg_win: float
    avg_loss: float
    expectancy: float
    equity_curve: list[dict[str, Any]]
    trades: list[Trade]
    blocked: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    n_bars: int = 0
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "params": self.params,
            "nBars": self.n_bars,
            "initialBalance": self.initial_balance,
            "finalEquity": round(self.final_equity, 2),
            "nTrades": self.n_trades,
            "wins": self.wins,
            "losses": self.losses,
            "winRate": round(self.win_rate, 4),
            "grossProfit": round(self.gross_profit, 2),
            "grossLoss": round(self.gross_loss, 2),
            "netPnl": round(self.net_pnl, 2),
            "profitFactor": None if self.profit_factor is None else round(self.profit_factor, 4),
            "maxDrawdownAbs": round(self.max_drawdown_abs, 2),
            "maxDrawdownPct": round(self.max_drawdown_pct, 4),
            "avgWin": round(self.avg_win, 2),
            "avgLoss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 2),
            "equityCurve": self.equity_curve,
            "trades": [t.to_dict() for t in self.trades],
            "blocked": self.blocked,
            "warnings": self.warnings,
        }

    def format_summary(self, currency: str = "USD") -> str:
        """Tableau récapitulatif en français."""
        pf = "∞" if (self.profit_factor is None and self.gross_profit > 0) else (
            "n/a" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        )
        rows = [
            ("Stratégie", f"{self.strategy} {self.params}"),
            ("Symbole", self.symbol),
            ("Bougies", str(self.n_bars)),
            ("Solde initial", f"{self.initial_balance:,.2f} {currency}"),
            ("Équité finale", f"{self.final_equity:,.2f} {currency}"),
            ("PnL net", f"{self.net_pnl:+,.2f} {currency}"),
            ("Trades", str(self.n_trades)),
            ("Gagnants / Perdants", f"{self.wins} / {self.losses}"),
            ("Taux de réussite", f"{self.win_rate * 100:.1f} %"),
            ("Profit brut", f"{self.gross_profit:,.2f} {currency}"),
            ("Perte brute", f"{self.gross_loss:,.2f} {currency}"),
            ("Facteur de profit", pf),
            ("Gain moyen", f"{self.avg_win:,.2f} {currency}"),
            ("Perte moyenne", f"{self.avg_loss:,.2f} {currency}"),
            ("Espérance / trade", f"{self.expectancy:+,.2f} {currency}"),
            ("Drawdown max", f"{self.max_drawdown_abs:,.2f} {currency} ({self.max_drawdown_pct * 100:.2f} %)"),
        ]
        if self.blocked:
            rows.append(("Signaux bloqués", ", ".join(f"{k}={v}" for k, v in sorted(self.blocked.items()))))
        width = max(len(label) for label, _ in rows)
        line = "=" * (width + 40)
        out = [line, " RÉSULTATS DU BACKTEST", line]
        out += [f" {label:<{width}} : {value}" for label, value in rows]
        out.append(line)
        if self.warnings:
            out.append(" Avertissements :")
            out += [f"  - {w}" for w in self.warnings[:20]]
            if len(self.warnings) > 20:
                out.append(f"  … {len(self.warnings) - 20} autre(s)")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #
class Backtester:
    """Exécute une stratégie sur une série de bougies."""

    def __init__(
        self,
        strategy: Strategy,
        candles: list[Candle],
        spec: SymbolSpec | None = None,
        risk: RiskConfig | None = None,
        initial_balance: float = 10_000.0,
        currency: str = "USD",
        leverage: float = 100.0,
    ) -> None:
        self.strategy = strategy
        self.candles = candles
        self.spec = spec or SymbolSpec.default_fx()
        self.risk = risk or RiskConfig()
        self.initial_balance = float(initial_balance)
        self.currency = currency
        self.leverage = leverage

    # -- calculs élémentaires ------------------------------------------------ #
    def _pnl(self, direction: str, entry: float, exit_: float, lots: float) -> float:
        sign = 1.0 if direction == "buy" else -1.0
        return sign * (exit_ - entry) * lots * self.spec.contract_size * self.risk.profit_ccy_to_account_rate

    def run(self) -> BacktestResult:
        """Lance le backtest et retourne les statistiques."""
        candles = self.candles
        spec = self.spec
        risk = self.risk
        point = spec.point
        n = len(candles)
        tf = infer_timeframe(candles)

        balance = self.initial_balance
        trades: list[Trade] = []
        warnings: list[str] = []
        blocked_counts: dict[str, int] = {}
        equity_curve: list[dict[str, Any]] = [
            {"time": candles[0].time.isoformat() if candles else None, "equity": round(balance, 2)}
        ]
        # Équité « mark-to-market » à chaque clôture (pour le drawdown réel).
        equity_track: list[float] = [balance]
        realized_by_day: dict[date, float] = {}
        day_start_balance: dict[date, float] = {}
        cache: dict[Any, Any] = {}

        def count_blocked(items: list[Blocked]) -> None:
            for b in items:
                blocked_counts[b.rule] = blocked_counts.get(b.rule, 0) + 1

        open_trade: _OpenTrade | None = None
        pending_entry: Signal | None = None
        pending_entry_reason = ""
        pending_exit: ExitSignal | None = None

        def close_trade(trade: _OpenTrade, exit_time: datetime, exit_price: float, reason: str) -> None:
            nonlocal balance, open_trade
            pnl = self._pnl(trade.direction, trade.entry_price, exit_price, trade.lots)
            balance += pnl
            trades.append(
                Trade(
                    direction=trade.direction,
                    entry_time=trade.entry_time,
                    entry_price=trade.entry_price,
                    exit_time=exit_time,
                    exit_price=exit_price,
                    lots=trade.lots,
                    pnl=pnl,
                    reason=reason,
                    entry_reason=trade.entry_reason,
                    sl=trade.sl,
                    tp=trade.tp,
                )
            )
            equity_curve.append({"time": exit_time.isoformat(), "equity": round(balance, 2)})
            equity_track.append(balance)
            day = exit_time.date()
            realized_by_day[day] = realized_by_day.get(day, 0.0) + pnl
            open_trade = None

        for i in range(n):
            bar = candles[i]
            spread = bar.spread_points * point
            day = bar.time.date()
            day_start_balance.setdefault(day, balance)

            # 1) Entrée en attente : exécution à l'ouverture de cette bougie.
            if pending_entry is not None and open_trade is None:
                sig = pending_entry
                pending_entry = None
                bid, ask = bar.open, bar.open + spread
                entry_price = ask if sig.side == "buy" else bid
                sl, tp = spec.round_price(sig.sl), spec.round_price(sig.tp)
                # Contrôles au moment de l'exécution, comme le cycle live au moment de l'ordre :
                # spread réellement payé et stops valides par rapport au bid/ask d'exécution.
                fill_blocked = guard_spread(bar.spread_points, risk.max_spread_points)
                fill_blocked += validate_stops(sig.side, sl, tp, bid, ask, spec)
                lots = 0.0
                if fill_blocked:
                    count_blocked(fill_blocked)
                    warnings.append(
                        f"{bar.time.isoformat()} : signal {sig.side} refusé à l'exécution : "
                        + " ; ".join(b.detail for b in fill_blocked)
                    )
                else:
                    sizing = compute_lots(balance, risk.risk_pct_per_trade, entry_price, sl, spec, risk.profit_ccy_to_account_rate)
                    if sizing.ok:
                        warnings.extend(f"{bar.time.isoformat()} : {w}" for w in sizing.warnings)
                        lots = sizing.lots
                        # Marge : sans position ouverte, la marge libre vaut le solde.
                        margin = check_margin(
                            lots, entry_price, spec.contract_size, self.leverage, balance,
                            risk.margin_usage_cap, spec.volume_step,
                        )
                        if not margin.ok:
                            reduced = round_lots_down(margin.max_lots, spec.volume_step)
                            if reduced >= spec.volume_min:
                                warnings.append(
                                    f"{bar.time.isoformat()} : volume réduit de {lots} à {reduced} lot(s) pour la marge : {margin.detail}"
                                )
                                lots = reduced
                            else:
                                count_blocked([Blocked("margin", margin.detail)])
                                warnings.append(f"{bar.time.isoformat()} : signal {sig.side} refusé (marge) : {margin.detail}")
                                lots = 0.0
                    else:
                        warnings.append(f"{bar.time.isoformat()} : signal {sig.side} ignoré ({sizing.reason})")
                if lots > 0:
                    open_trade = _OpenTrade(
                        direction=sig.side,
                        entry_time=bar.time,
                        entry_price=spec.round_price(entry_price) or entry_price,
                        lots=lots,
                        sl=sl,
                        tp=tp,
                        entry_reason=pending_entry_reason,
                        entry_index=i,
                    )

            # 2) Sortie discrétionnaire en attente : exécution à l'ouverture.
            if pending_exit is not None and open_trade is not None:
                exit_price = bar.open if open_trade.direction == "buy" else bar.open + spread
                close_trade(open_trade, bar.time, exit_price, pending_exit.reason or "sortie stratégie")
            pending_exit = None

            # 3) SL / TP en intrabar (SL prioritaire si les deux sont touchés).
            if open_trade is not None:
                t = open_trade
                # Un stop déclenché devient un ordre au marché : si la bougie ouvre
                # déjà au-delà du niveau (gap), la sortie se fait à l'ouverture.
                if t.direction == "buy":
                    sl_hit = t.sl is not None and bar.low <= t.sl + 1e-12
                    tp_hit = t.tp is not None and bar.high >= t.tp - 1e-12
                    sl_exit = min(bar.open, float(t.sl)) if sl_hit else None
                    tp_exit = max(bar.open, float(t.tp)) if tp_hit else None
                else:
                    sl_hit = t.sl is not None and bar.high + spread >= t.sl - 1e-12
                    tp_hit = t.tp is not None and bar.low + spread <= t.tp + 1e-12
                    sl_exit = max(bar.open + spread, float(t.sl)) if sl_hit else None
                    tp_exit = min(bar.open + spread, float(t.tp)) if tp_hit else None
                if sl_hit:
                    gap = abs(sl_exit - float(t.sl)) > 1e-12
                    close_trade(t, bar.time, spec.round_price(sl_exit) or sl_exit, "stop loss (gap)" if gap else "stop loss")
                elif tp_hit:
                    gap = abs(tp_exit - float(t.tp)) > 1e-12
                    close_trade(t, bar.time, spec.round_price(tp_exit) or tp_exit, "take profit (gap)" if gap else "take profit")
                if open_trade is not None:
                    # Valorisation à la clôture (achat au bid = close ; vente au ask = close + spread).
                    mark = bar.close if t.direction == "buy" else bar.close + spread
                    equity_track.append(balance + self._pnl(t.direction, t.entry_price, mark, t.lots))

            # 4) Décisions à la clôture de la bougie i.
            if i + 1 < self.strategy.warmup_bars:
                continue
            account = AccountState(equity=balance, balance=balance, margin_free=balance, leverage=self.leverage, currency=self.currency)
            position = None
            if open_trade is not None:
                position = PositionState(
                    direction=open_trade.direction,
                    volume_lots=open_trade.lots,
                    price_open=open_trade.entry_price,
                    sl=open_trade.sl,
                    tp=open_trade.tp,
                    time_open=open_trade.entry_time,
                    position_id=len(trades) + 1,
                    symbol=spec.symbol,
                    price_current=bar.close,
                )
            ctx = Context(candles=candles, index=i, spec=spec, position=position, account=account, cache=cache)

            if open_trade is not None:
                exit_signal = self.strategy.should_exit(ctx)
                if exit_signal is not None:
                    pending_exit = exit_signal
                else:
                    new_levels = self.strategy.manage(ctx)
                    if new_levels:
                        if "sl" in new_levels:
                            open_trade.sl = spec.round_price(new_levels.get("sl"))
                        if "tp" in new_levels:
                            open_trade.tp = spec.round_price(new_levels.get("tp"))
                continue

            signal = self.strategy.on_bar(ctx)
            if signal is None:
                continue
            decision_time = bar.time + tf if tf else bar.time
            blocked: list[Blocked] = []
            blocked += guard_spread(bar.spread_points, risk.max_spread_points)
            blocked += guard_trading_hours(decision_time, risk.trading_hours_utc)
            blocked += guard_friday_cutoff(decision_time, risk.no_new_trades_friday_after_hour_utc)
            blocked += guard_daily_loss_amount(
                realized_by_day.get(day, 0.0), day_start_balance.get(day, balance), risk.max_daily_loss_pct
            )
            if blocked:
                count_blocked(blocked)
                continue
            pending_entry = signal
            pending_entry_reason = signal.reason

        # Clôture forcée en fin de données (valorisation à la dernière clôture).
        if open_trade is not None and n:
            last = candles[-1]
            spread = last.spread_points * point
            exit_price = last.close if open_trade.direction == "buy" else last.close + spread
            close_trade(open_trade, candle_close_time(candles), exit_price, "fin des données")

        return self._stats(trades, balance, equity_curve, blocked_counts, warnings, n, equity_track)

    # -- statistiques -------------------------------------------------------- #
    def _stats(
        self,
        trades: list[Trade],
        final_balance: float,
        equity_curve: list[dict[str, Any]],
        blocked: dict[str, int],
        warnings: list[str],
        n_bars: int,
        equity_track: list[float] | None = None,
    ) -> BacktestResult:
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)  # négatif ou nul
        n = len(trades)
        peak = -float("inf")
        max_dd = 0.0
        max_dd_pct = 0.0
        # Drawdown sur l'équité mark-to-market (repli : points de clôture de trade).
        track = equity_track if equity_track is not None else [float(pt["equity"]) for pt in equity_curve]
        for eq in track:
            peak = max(peak, eq)
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd / peak if peak > 0 else 0.0
        profit_factor: float | None
        if gross_loss < 0:
            profit_factor = gross_profit / abs(gross_loss)
        else:
            profit_factor = None  # aucune perte : facteur infini (non sérialisable)
        return BacktestResult(
            strategy=self.strategy.name,
            symbol=self.spec.symbol,
            initial_balance=self.initial_balance,
            final_equity=final_balance,
            n_trades=n,
            wins=len(wins),
            losses=len(losses),
            win_rate=(len(wins) / n) if n else 0.0,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            net_pnl=final_balance - self.initial_balance,
            profit_factor=profit_factor,
            max_drawdown_abs=max_dd,
            max_drawdown_pct=max_dd_pct,
            avg_win=(gross_profit / len(wins)) if wins else 0.0,
            avg_loss=(gross_loss / len(losses)) if losses else 0.0,
            expectancy=((final_balance - self.initial_balance) / n) if n else 0.0,
            equity_curve=equity_curve,
            trades=trades,
            blocked=blocked,
            warnings=warnings,
            n_bars=n_bars,
            params=dict(self.strategy.params),
        )


def run_backtest(
    strategy: Strategy,
    candles: list[Candle],
    spec: SymbolSpec | None = None,
    risk: RiskConfig | None = None,
    initial_balance: float = 10_000.0,
) -> BacktestResult:
    """Raccourci fonctionnel autour de ``Backtester``."""
    return Backtester(strategy, candles, spec=spec, risk=risk, initial_balance=initial_balance).run()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class FrenchHelpFormatter(argparse.HelpFormatter):
    """Formateur d'aide argparse avec le préfixe « utilisation » en français."""

    def _format_usage(self, usage, actions, groups, prefix):  # type: ignore[override]
        if prefix is None:
            prefix = "utilisation : "
        return super()._format_usage(usage, actions, groups, prefix)


def french_parser(prog: str, description: str, epilog: str) -> argparse.ArgumentParser:
    """Crée un ``ArgumentParser`` dont tous les libellés automatiques sont en français."""
    parser = argparse.ArgumentParser(
        prog=prog, description=description, epilog=epilog, add_help=False, formatter_class=FrenchHelpFormatter,
    )
    parser._positionals.title = "arguments"
    parser._optionals.title = "paramètres"
    parser.add_argument("-h", "--help", action="help", help="afficher cette aide et quitter")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = french_parser(
        prog="python3 -m mt5.engine.backtest",
        description="Backteste une stratégie sur un fichier de bougies au format Axi MT5.",
        epilog="Exemple : python3 -m mt5.engine.backtest --strategy ema_cross "
        "--candles mt5/data/EURUSD_H1_sample.json --symbol EURUSD --balance 10000",
    )
    parser.add_argument("--strategy", required=True, help="nom de la stratégie (module de mt5/strategies), ex. ema_cross")
    parser.add_argument("--candles", required=True, help="fichier JSON de bougies (format Axi, plus ancienne en premier)")
    parser.add_argument("--symbol", default="EURUSD", help="symbole (défaut : EURUSD)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="solde initial dans la devise du compte (défaut : 10000)")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="risque par trade en %% de l'équité (défaut : 1)")
    parser.add_argument("--params", default=None, help="paramètres de stratégie, ex. fast=9,slow=21")
    parser.add_argument("--spec", default=None, help="fichier JSON de spécification du symbole (défaut : FX 5 décimales)")
    parser.add_argument("--json", dest="json_out", default=None, help="écrire le résultat complet (stats + trades) dans ce fichier JSON")
    parser.add_argument("--max-spread-points", type=float, default=None, help="spread maximal accepté à l'entrée (points)")
    parser.add_argument("--hours", default=None, help="plage horaire UTC d'ouverture, ex. 7-21 (défaut : toujours)")
    parser.add_argument("--max-daily-loss-pct", type=float, default=3.0, help="perte journalière max en %% du solde (défaut : 3)")
    parser.add_argument("--friday-cutoff-hour", type=int, default=20, help="plus d'ouverture le vendredi à partir de cette heure UTC (défaut : 20 ; -1 pour désactiver)")
    parser.add_argument("--rate", type=float, default=1.0, help="taux devise de profit → devise du compte (défaut : 1)")
    parser.add_argument("--currency", default="USD", help="devise du compte pour l'affichage (défaut : USD)")
    return parser


def parse_hours(text: str | None) -> tuple[int, int] | None:
    """``"7-21"`` → ``(7, 21)`` ; ``None``/vide → ``None``."""
    if not text:
        return None
    parts = text.replace("h", "").split("-")
    if len(parts) != 2:
        raise ValueError(f"Plage horaire invalide (attendu debut-fin, ex. 7-21) : {text!r}")
    start, end = int(parts[0]), int(parts[1])
    if not (0 <= start <= 24 and 0 <= end <= 24):
        raise ValueError("Les heures doivent être comprises entre 0 et 24")
    return (start % 24, end % 24)


def load_spec(path: str | None, symbol: str) -> SymbolSpec:
    """Charge ``symbol.json`` (ou une liste de marchés dont on extrait ``symbol``)."""
    if not path:
        return SymbolSpec.default_fx(symbol)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, list):
        matches = [p for p in payload if isinstance(p, dict) and str(p.get("symbol", "")).upper() == symbol.upper()]
        if not matches:
            raise ValueError(f"Symbole {symbol} absent de {path}")
        payload = matches[0]
    elif isinstance(payload, dict) and "markets" in payload and isinstance(payload["markets"], list):
        return load_spec_from_list(payload["markets"], symbol, path)
    return SymbolSpec.from_dict(payload, symbol=symbol)


def load_spec_from_list(items: list[Any], symbol: str, path: str) -> SymbolSpec:
    matches = [p for p in items if isinstance(p, dict) and str(p.get("symbol", "")).upper() == symbol.upper()]
    if not matches:
        raise ValueError(f"Symbole {symbol} absent de {path}")
    return SymbolSpec.from_dict(matches[0], symbol=symbol)


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI. Retourne 0 si OK, 2 si entrée invalide."""
    from mt5.strategies.registry import get_strategy

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        candles = load_candles(args.candles)
        if not candles:
            raise ValueError(f"Aucune bougie dans {args.candles}")
        spec = load_spec(args.spec, args.symbol)
        strategy = get_strategy(args.strategy, parse_params_string(args.params))
        risk = RiskConfig(
            risk_pct_per_trade=args.risk_pct,
            max_daily_loss_pct=args.max_daily_loss_pct,
            max_spread_points=args.max_spread_points,
            trading_hours_utc=parse_hours(args.hours),
            no_new_trades_friday_after_hour_utc=None if args.friday_cutoff_hour is not None and args.friday_cutoff_hour < 0 else args.friday_cutoff_hour,
            profit_ccy_to_account_rate=args.rate,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erreur d'entrée : {exc}", file=sys.stderr)
        return 2

    result = Backtester(strategy, candles, spec=spec, risk=risk, initial_balance=args.balance, currency=args.currency).run()
    print(result.format_summary(args.currency))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2, ensure_ascii=False)
        print(f"Résultat détaillé écrit dans {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
