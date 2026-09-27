"""Un cycle de décision live : SANS ÉTAT, SANS RÉSEAU.

Entrées (fichiers JSON écrits par Claude à partir du connecteur Axi MT5) :
``candles.json``, ``positions.json``, ``account.json``, ``quote.json``,
``symbol.json`` et, en option, ``fills.json`` (exécutions du jour).

Sortie : un JSON d'actions que Claude exécute ensuite via le connecteur ::

    {"schemaVersion": 1, "asOf": "...", "symbol": "EURUSD", "strategy": "ema_cross",
     "account": {"equity": ..., "currency": "USD"},
     "actions": [{"type": "open", ...} | {"type": "close", ...} | {"type": "modify", ...}],
     "blocked": [{"rule": "...", "detail": "..."}], "warnings": ["..."],
     "lastCandle": {...}}

Les niveaux ``priceSL``/``priceTP`` des actions sont toujours des nombres :
``0.0`` signifie « pas de niveau » (convention de ``modify_mt5_position``).
Les stops sont validés comme par le serveur MT5 (achat : SL sous le bid, TP
au-dessus ; vente : SL au-dessus du ask, TP en dessous ; distance ≥ stopsLevel).

« Maintenant » (gardes horaires, date de la perte journalière) est pris dans
l'ordre : ``--now``, ``account.asOf`` (heure de lecture du compte, donc de la
décision), ``quote.time`` (heure du dernier tick, potentiellement périmée),
horloge système.

Règle de ré-entrée sans état : le signal de la dernière bougie terminée est
considéré CONSOMMÉ si une position ouverte sur le symbole a un ``timeCreate``
supérieur ou égal à l'heure d'ouverture de cette bougie. Jamais plus d'une
action ``open`` par cycle.

Code de sortie : 0 dès que la décision a été calculée (même si tout est
bloqué) ; non nul uniquement sur entrée invalide.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backtest import french_parser, load_spec, parse_hours
from .candles import Candle, candle_close_time, candles_from_records, load_candles, parse_time
from .risk import (
    Blocked,
    RiskConfig,
    check_margin,
    compute_lots,
    guard_daily_loss,
    guard_friday_cutoff,
    guard_max_positions,
    guard_session,
    guard_spread,
    guard_trade_mode,
    guard_trading_hours,
    round_lots_down,
    validate_stops,
)
from .strategy import (
    AccountState,
    Context,
    PositionState,
    Strategy,
    SymbolSpec,
    parse_params_string,
    positions_from_records,
)

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Cotation
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    """Cotation courante (format ``get_mt5_quote``). Tous les champs sont optionnels."""

    bid: float | None = None
    ask: float | None = None
    spread_points: float | None = None
    time: datetime | None = None
    trade_mode: str | None = None
    session_open: bool | None = None
    session_opens: str | None = None

    @classmethod
    def from_dict(cls, data: Any, spec: SymbolSpec) -> "Quote":
        if not isinstance(data, dict):
            return cls()
        bid = _float_or_none(data.get("bid"))
        ask = _float_or_none(data.get("ask"))
        point = _float_or_none(data.get("point")) or spec.point
        spread_price = _float_or_none(data.get("spread"))
        spread_points: float | None = None
        if spread_price is not None and point:
            spread_points = round(spread_price / point, 1)
        elif bid is not None and ask is not None and point:
            spread_points = round((ask - bid) / point, 1)
        t_raw = data.get("time")
        when: datetime | None = None
        if t_raw:
            try:
                when = parse_time(t_raw)
            except ValueError:
                when = None
        mode = data.get("tradeMode")
        session = data.get("tradeSessionOpen")
        return cls(
            bid=bid,
            ask=ask,
            spread_points=spread_points,
            time=when,
            trade_mode=str(mode).strip().lower() if mode is not None else None,
            session_open=bool(session) if isinstance(session, bool) else None,
            session_opens=str(data.get("tradeSessionOpens")) if data.get("tradeSessionOpens") else None,
        )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


# --------------------------------------------------------------------------- #
# Cycle
# --------------------------------------------------------------------------- #
def _levels_changed(old: float | None, new: float | None, point: float) -> bool:
    """Vrai si le niveau change d'au moins 1 point (ou passe de/vers None)."""
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return abs(new - old) >= point - 1e-12


def _level_or_zero(level: float | None) -> float:
    """``None`` → ``0.0`` : le connecteur exige un nombre (0 = pas de niveau)."""
    return 0.0 if level is None else float(level)


def run_cycle(
    strategy: Strategy,
    symbol: str,
    candles: list[Candle],
    positions_raw: Any,
    account_raw: Any,
    quote_raw: Any,
    spec: SymbolSpec,
    risk: RiskConfig,
    fills_raw: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Calcule les actions d'un cycle. Lève ``ValueError`` sur entrée invalide."""
    if not candles:
        raise ValueError("aucune bougie fournie : impossible de décider")
    warnings: list[str] = []
    blocked: list[Blocked] = []
    actions: list[dict[str, Any]] = []

    positions, pos_warnings = positions_from_records(positions_raw, symbol)
    warnings.extend(pos_warnings)
    account = AccountState.from_dict(account_raw if isinstance(account_raw, dict) else {})
    if not isinstance(account_raw, dict):
        warnings.append("account.json absent ou invalide : équité inconnue (0)")
    quote = Quote.from_dict(quote_raw, spec)
    if quote_raw is None:
        warnings.append("quote.json absent : spread et session inconnus")

    # account.asOf = heure de la décision ; quote.time = heure du dernier tick (peut être périmé).
    now = now or account.as_of or quote.time or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)

    last = candles[-1]
    index = len(candles) - 1
    as_of = candle_close_time(candles)
    point = spec.point

    # Bid/ask de référence pour valider les stops (repli : dernière clôture + spread de la bougie).
    spread_points = quote.spread_points if quote.spread_points is not None else float(last.spread_points)
    bid = quote.bid if quote.bid else last.close
    ask = quote.ask if quote.ask else last.close + spread_points * point

    if fills_raw is None and risk.max_daily_loss_pct and risk.max_daily_loss_pct > 0:
        warnings.append("fills.json absent : perte journalière non contrôlée (règle max_daily_loss inactive ce cycle)")

    enough_bars = len(candles) >= strategy.warmup_bars
    if not enough_bars:
        warnings.append(
            f"seulement {len(candles)} bougie(s) pour une chauffe de {strategy.warmup_bars} : aucune décision de stratégie"
        )
    if quote.trade_mode is None and spec.trade_mode is None:
        warnings.append("tradeMode inconnu (null) : aucune restriction appliquée")

    cache: dict[Any, Any] = {}

    # ---- positions ouvertes : sortie ou gestion -------------------------- #
    if enough_bars:
        for pos in positions:
            ctx = Context(candles=candles, index=index, spec=spec, position=pos, account=account, cache=cache)
            exit_signal = strategy.should_exit(ctx)
            if exit_signal is not None:
                if quote.session_open is False:
                    warnings.append(f"session fermée : la clôture de {pos.position_id} sera exécutée à la réouverture")
                actions.append({"type": "close", "positionId": pos.position_id, "reason": exit_signal.reason or "sortie stratégie"})
                continue
            new_levels = strategy.manage(ctx)
            if not new_levels:
                continue
            new_sl = spec.round_price(new_levels.get("sl", pos.sl))
            new_tp = spec.round_price(new_levels.get("tp", pos.tp))
            if not (_levels_changed(pos.sl, new_sl, point) or _levels_changed(pos.tp, new_tp, point)):
                continue
            stops_block = validate_stops(pos.direction, new_sl, new_tp, bid, ask, spec, label=f"modification de {pos.position_id}")
            if stops_block:
                blocked += stops_block
                warnings.append(f"modification de {pos.position_id} non émise : SL/TP invalides par rapport au bid/ask courant")
                continue
            if quote.session_open is False:
                warnings.append(f"session fermée : la modification de {pos.position_id} sera exécutée à la réouverture")
            actions.append(
                {
                    "type": "modify",
                    "positionId": pos.position_id,
                    "priceSL": _level_or_zero(new_sl),
                    "priceTP": _level_or_zero(new_tp),
                    "reason": f"ajustement SL/TP par la stratégie (SL {pos.sl} → {new_sl}, TP {pos.tp} → {new_tp})",
                }
            )

    # ---- ouverture ------------------------------------------------------- #
    consumed = any(p.time_open is not None and p.time_open >= last.time for p in positions)
    if consumed:
        warnings.append(
            f"signal de la bougie {last.time.isoformat()} déjà consommé (position créée après son ouverture) : pas de nouvelle ouverture"
        )
    max_pos_block = guard_max_positions(positions, symbol, risk.max_positions_per_symbol)

    if enough_bars and not consumed and not max_pos_block:
        ctx = Context(candles=candles, index=index, spec=spec, position=None, account=account, cache=cache)
        signal = strategy.on_bar(ctx)
        if signal is not None:
            trade_mode = quote.trade_mode if quote.trade_mode is not None else spec.trade_mode
            blocked += guard_session(quote.session_open, quote.session_opens)
            blocked += guard_trade_mode(trade_mode, signal.side)
            blocked += guard_spread(spread_points, risk.max_spread_points)
            blocked += guard_trading_hours(now, risk.trading_hours_utc)
            blocked += guard_friday_cutoff(now, risk.no_new_trades_friday_after_hour_utc)
            daily_block, daily_warnings = guard_daily_loss(fills_raw, account.balance or account.equity, risk.max_daily_loss_pct, now.date())
            blocked += daily_block
            warnings.extend(daily_warnings)

            entry_price = ask if signal.side == "buy" else bid
            sl = spec.round_price(signal.sl)
            tp = spec.round_price(signal.tp)
            # Validation côté serveur MT5 : un achat se clôture au bid, une vente au ask.
            blocked += validate_stops(signal.side, sl, tp, bid, ask, spec)

            if not blocked:
                sizing = compute_lots(account.equity, risk.risk_pct_per_trade, entry_price, sl, spec, risk.profit_ccy_to_account_rate)
                warnings.extend(sizing.warnings)
                if not sizing.ok:
                    blocked.append(Blocked("lot_sizing", sizing.reason or "taille de position nulle"))
                else:
                    lots = sizing.lots
                    margin = check_margin(
                        lots, entry_price, spec.contract_size, account.leverage, account.margin_free,
                        risk.margin_usage_cap, spec.volume_step,
                    )
                    if not margin.ok:
                        reduced = round_lots_down(margin.max_lots, spec.volume_step)
                        if reduced >= spec.volume_min:
                            warnings.append(f"volume réduit de {lots} à {reduced} lot(s) pour la marge : {margin.detail}")
                            lots = reduced
                        else:
                            blocked.append(Blocked("margin", margin.detail))
                    if not blocked:
                        actions.append(
                            {
                                "type": "open",
                                "direction": signal.side,
                                "volumeLots": lots,
                                "priceSL": _level_or_zero(sl),
                                "priceTP": _level_or_zero(tp),
                                "reason": signal.reason or "signal stratégie",
                            }
                        )
            if blocked:
                warnings.append(f"signal {signal.side} ({signal.reason}) bloqué par {len(blocked)} règle(s)")
    elif enough_bars and max_pos_block and not consumed:
        # Position(s) déjà ouverte(s) : on ne consulte pas on_bar mais on rend la règle visible.
        blocked += max_pos_block

    return {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": as_of.isoformat(),
        "now": now.isoformat(),
        "symbol": symbol,
        "strategy": strategy.name,
        "params": dict(strategy.params),
        "account": {
            "equity": account.equity,
            "balance": account.balance,
            "marginFree": account.margin_free,
            "leverage": account.leverage,
            "currency": account.currency,
        },
        "positions": len(positions),
        "actions": actions,
        "blocked": [b.to_dict() for b in blocked],
        "warnings": warnings,
        "lastCandle": last.to_dict(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json(path: str | None, label: str, required: bool = True) -> Any:
    if not path:
        if required:
            raise ValueError(f"fichier {label} requis")
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            raise ValueError(f"fichier {label} introuvable : {path}")
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"fichier {label} illisible ({path}) : {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = french_parser(
        prog="python3 -m mt5.engine.cycle",
        description=(
            "Calcule les actions d'un cycle de trading (sans état, sans réseau) à partir des "
            "fichiers JSON produits par le connecteur Axi MT5 et écrit un JSON d'actions."
        ),
        epilog=(
            "Exemple : python3 -m mt5.engine.cycle --strategy ema_cross --symbol EURUSD "
            "--candles candles.json --positions positions.json --account account.json "
            "--quote quote.json --spec symbol.json --out actions.json"
        ),
    )
    parser.add_argument("--strategy", required=True, help="nom de la stratégie (module de mt5/strategies)")
    parser.add_argument("--symbol", required=True, help="symbole traité, ex. EURUSD")
    parser.add_argument("--candles", required=True, help="bougies terminées (get_mt5_candles), plus ancienne en premier")
    parser.add_argument("--positions", required=True, help="positions ouvertes (get_mt5_positions) ; liste vide acceptée")
    parser.add_argument("--account", required=True, help="état du compte (get_account)")
    parser.add_argument("--quote", default=None, help="cotation courante (get_mt5_quote)")
    parser.add_argument("--spec", default=None, help="spécification du symbole (entrée de get_mt5_available_markets)")
    parser.add_argument("--fills", default=None, help="exécutions du jour (get_mt5_fills), pour la perte journalière")
    parser.add_argument("--params", default=None, help="paramètres de stratégie, ex. fast=9,slow=21")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="risque par trade en %% de l'équité (défaut : 1)")
    parser.add_argument("--max-daily-loss-pct", type=float, default=3.0, help="perte journalière max en %% du solde (défaut : 3)")
    parser.add_argument("--max-spread-points", type=float, default=None, help="spread maximal accepté à l'ouverture (points)")
    parser.add_argument("--hours", default=None, help="plage horaire UTC d'ouverture, ex. 7-21 (défaut : toujours)")
    parser.add_argument("--max-positions", type=int, default=1, help="positions max sur le symbole (défaut : 1)")
    parser.add_argument("--friday-cutoff-hour", type=int, default=20, help="plus d'ouverture le vendredi à partir de cette heure UTC (défaut : 20 ; -1 pour désactiver)")
    parser.add_argument("--margin-cap", type=float, default=0.8, help="fraction max de la marge libre utilisable (défaut : 0.8)")
    parser.add_argument("--rate", type=float, default=1.0, help="taux devise de profit → devise du compte (défaut : 1)")
    parser.add_argument("--now", default=None, help="horodatage ISO à utiliser comme « maintenant » (défaut : asOf du compte, sinon heure de la cotation, sinon horloge)")
    parser.add_argument("--out", default=None, help="fichier de sortie des actions (JSON)")
    parser.add_argument("--quiet", action="store_true", help="ne pas afficher le JSON sur la sortie standard")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI. 0 = décision calculée ; 2 = entrée invalide."""
    from mt5.strategies.registry import get_strategy

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        candles = load_candles(args.candles)
        if not candles:
            raise ValueError(f"aucune bougie dans {args.candles}")
        positions_raw = _load_json(args.positions, "positions")
        account_raw = _load_json(args.account, "account")
        quote_raw = _load_json(args.quote, "quote", required=False)
        fills_raw = _load_json(args.fills, "fills", required=False)
        spec = load_spec(args.spec, args.symbol)
        strategy = get_strategy(args.strategy, parse_params_string(args.params))
        risk = RiskConfig(
            risk_pct_per_trade=args.risk_pct,
            max_positions_per_symbol=args.max_positions,
            max_daily_loss_pct=args.max_daily_loss_pct,
            max_spread_points=args.max_spread_points,
            trading_hours_utc=parse_hours(args.hours),
            no_new_trades_friday_after_hour_utc=None if args.friday_cutoff_hour is not None and args.friday_cutoff_hour < 0 else args.friday_cutoff_hour,
            margin_usage_cap=args.margin_cap,
            profit_ccy_to_account_rate=args.rate,
        )
        now = parse_time(args.now) if args.now else None
        result = run_cycle(
            strategy, args.symbol, candles, positions_raw, account_raw, quote_raw, spec, risk,
            fills_raw=fills_raw, now=now,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erreur d'entrée : {exc}", file=sys.stderr)
        return 2

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if not args.quiet:
        print(text)
    return 0


__all__ = ["Quote", "run_cycle", "main", "SCHEMA_VERSION"]

if __name__ == "__main__":
    sys.exit(main())
