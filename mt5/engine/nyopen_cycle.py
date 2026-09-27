"""Cycle live de la stratégie « NY Open 9:31 » — deux sous-commandes, sans réseau.

``plan``      : à la clôture de la bougie de référence, calcule les deux ordres stop
                (prix, SL, TP, lots, expiration) prêts pour ``place_mt5_order``.
``supervise`` : pendant la fenêtre d'entrée puis la vie de la position, décide les
                actions (annuler l'ordre opposé, fermer une seconde position, fermer
                au bout de 60 min) à partir de positions.json / orders.json (+ fills.json).

Exemples (depuis la racine du dépôt) :

  python3 -m mt5.engine.nyopen_cycle plan --ref-candle runtime/candles.json \\
      --quote runtime/quote.json --spec runtime/symbol.json --account runtime/account.json \\
      --out runtime/plan.json

  python3 -m mt5.engine.nyopen_cycle supervise --positions runtime/positions.json \\
      --orders runtime/orders.json --fills runtime/fills.json --day 2026-09-28 \\
      --out runtime/actions.json

Code de sortie : 0 décision calculée (même si le jour est sauté), 2 entrée invalide.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from mt5.engine.backtest import french_parser
from mt5.engine.candles import Candle, candle_from_dict, candles_from_records, parse_time
from mt5.engine.strategy import AccountState, SymbolSpec, parse_params_string, positions_from_records
from mt5.strategies import ny_open_931 as ny

SCHEMA_VERSION = 1


class InputError(Exception):
    """Entrée invalide (fichier illisible, champ manquant…)."""


def _read_json(path: str | None, label: str, required: bool = True) -> Any:
    if not path:
        if required:
            raise InputError(f"{label} : chemin manquant")
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise InputError(f"{label} : fichier introuvable ({path})") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} : JSON invalide ({exc})") from exc


def _now(value: str | None) -> datetime:
    if value:
        return parse_time(value)
    return datetime.now(timezone.utc)


def _write_out(payload: dict[str, Any], out: str | None, quiet: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
    if not quiet:
        print(text)


def _candles_from_payload(payload: Any) -> list[Candle]:
    if isinstance(payload, dict) and "time" in payload:
        return [candle_from_dict(payload)]
    if isinstance(payload, dict):
        for key in ("candles", "bars", "data"):
            if isinstance(payload.get(key), list):
                return candles_from_records(payload[key])
        raise InputError("ref-candle : liste de bougies attendue")
    if isinstance(payload, list):
        return candles_from_records(payload)
    raise InputError("ref-candle : format inconnu")


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def cmd_plan(args: argparse.Namespace) -> int:
    cfg = ny.NyOpenConfig.from_params(parse_params_string(args.params))
    if args.symbol:
        cfg.symbol = args.symbol
    candles = _candles_from_payload(_read_json(args.ref_candle, "ref-candle"))
    if not candles:
        raise InputError("ref-candle : aucune bougie")
    spec_raw = _read_json(args.spec, "spec", required=False)
    spec = SymbolSpec.from_dict(spec_raw, cfg.symbol) if spec_raw else _default_gold_spec(cfg.symbol)
    account = AccountState.from_dict(_read_json(args.account, "account"))
    quote = _read_json(args.quote, "quote", required=False)
    now = _now(args.now)

    day = date.fromisoformat(args.day) if args.day else ny.local_day(candles[-1].time, cfg)
    ref = ny.find_ref_bar(candles, day, cfg)
    if ref is None and len(candles) == 1:
        ref = candles[0]  # plan() vérifiera l'heure et expliquera le refus
    bid = ask = None
    spread: float | None = None
    if isinstance(quote, dict):
        bid = float(quote["bid"]) if quote.get("bid") is not None else None
        ask = float(quote["ask"]) if quote.get("ask") is not None else None
        if quote.get("spread") is not None:
            spread = float(quote["spread"])
        elif bid is not None and ask is not None:
            spread = ask - bid
    if ref is not None and spread is None:
        spread = ref.spread_points * spec.point

    equity = account.equity if args.equity is None else float(args.equity)
    if ref is None:
        plan_dict: dict[str, Any] = {
            "day": day.isoformat(), "refBar": None, "orders": [],
            "expiration": ny.orders_expiration(day, cfg).isoformat(),
            "hardDeadline": ny.hard_deadline(day, cfg).isoformat(),
            "skipped": f"aucune bougie ouvrant à {ny.ref_bar_start(day, cfg).isoformat()} dans ref-candle",
            "warnings": [],
        }
    else:
        plan_dict = ny.plan(ref, spread or 0.0, equity, spec, cfg, bid=bid, ask=ask, now=now).to_dict(cfg.symbol)

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "strategy": ny.NAME,
        "symbol": cfg.symbol,
        "now": now.isoformat(),
        "account": {"equity": equity, "currency": account.currency, "leverage": account.leverage},
        "quote": {"bid": bid, "ask": ask, "spread": spread},
        "params": cfg.to_dict(),
        "plan": plan_dict,
    }
    _write_out(payload, args.out, args.quiet)
    return 0


def _default_gold_spec(symbol: str) -> SymbolSpec:
    """Spécification XAUUSD Axi par défaut (2 décimales, contrat 100 oz)."""
    return SymbolSpec(
        symbol=symbol, digits=2, point=0.01, contract_size=100.0, currency_base="XAU",
        currency_profit="USD", volume_min=0.01, volume_max=20.0, volume_step=0.01,
        stops_level=1, freeze_level=0, trade_mode="full",
    )


# --------------------------------------------------------------------------- #
# supervise
# --------------------------------------------------------------------------- #
def cmd_supervise(args: argparse.Namespace) -> int:
    cfg = ny.NyOpenConfig.from_params(parse_params_string(args.params))
    if args.symbol:
        cfg.symbol = args.symbol
    now = _now(args.now)
    day = date.fromisoformat(args.day) if args.day else ny.local_day(now, cfg)

    pos_raw = _read_json(args.positions, "positions")
    positions, pos_warn = positions_from_records(pos_raw if pos_raw is not None else [])
    ord_raw = _read_json(args.orders, "orders", required=False)
    orders: list[ny.PendingOrderState] = []
    ord_warn: list[str] = []
    for entry in (ord_raw or []):
        try:
            orders.append(ny.PendingOrderState.from_dict(entry))
        except (ValueError, TypeError) as exc:
            ord_warn.append(f"ordre ignoré : {exc}")
    fills = _read_json(args.fills, "fills", required=False)
    trade_done = ny.trade_done_from_fills(fills, day, cfg) if fills is not None else False

    result = ny.supervise(now, positions, orders, day, cfg, trade_done=trade_done)
    result.warnings = list(pos_warn) + ord_warn + result.warnings
    if fills is None:
        result.warnings.append("fills.json absent : impossible de savoir si le trade du jour est déjà clos")
    payload = {"schemaVersion": SCHEMA_VERSION, "strategy": ny.NAME, "symbol": cfg.symbol, "params": cfg.to_dict()}
    payload.update(result.to_dict())
    _write_out(payload, args.out, args.quiet)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = french_parser(
        "python3 -m mt5.engine.nyopen_cycle",
        "Cycle live « NY Open 9:31 » : planification des ordres stop puis supervision (sans réseau).",
        "Les fichiers JSON sont ceux écrits depuis le connecteur Axi MT5 (formats natifs).",
    )
    sub = parser.add_subparsers(dest="command", metavar="commande")
    sub.required = True

    p = sub.add_parser("plan", help="calculer les deux ordres stop à la clôture de la bougie 9h31", add_help=False)
    p.add_argument("-h", "--help", action="help", help="afficher cette aide et quitter")
    p.add_argument("--ref-candle", required=True, help="bougies M1 du jour (liste) ou la seule bougie de référence")
    p.add_argument("--account", required=True, help="account.json (get_account)")
    p.add_argument("--quote", help="quote.json (get_mt5_quote) : bid/ask pour le contrôle de retard et le spread")
    p.add_argument("--spec", help="symbol.json (get_mt5_available_markets) ; défaut : XAUUSD Axi")
    p.add_argument("--symbol", help="symbole (défaut : paramètre symbol de la stratégie, XAUUSD)")
    p.add_argument("--day", help="date locale New York AAAA-MM-JJ (défaut : date de la dernière bougie)")
    p.add_argument("--now", help="instant courant ISO 8601 (défaut : horloge système UTC)")
    p.add_argument("--equity", type=float, help="forcer l'équité utilisée pour la taille")
    p.add_argument("--params", help="paramètres clé=valeur séparés par des virgules (ex. buffer_buy=0.1,rr=2)")
    p.add_argument("--out", help="écrire le plan JSON dans ce fichier")
    p.add_argument("--quiet", action="store_true", help="ne pas afficher le JSON")
    p.set_defaults(func=cmd_plan)

    s = sub.add_parser("supervise", help="décider annulations / fermetures pendant la séance", add_help=False)
    s.add_argument("-h", "--help", action="help", help="afficher cette aide et quitter")
    s.add_argument("--positions", required=True, help="positions.json (get_mt5_positions)")
    s.add_argument("--orders", help="orders.json (get_mt5_orders)")
    s.add_argument("--fills", help="fills.json du jour (get_mt5_fills) pour détecter un trade déjà clos")
    s.add_argument("--symbol", help="symbole (défaut : XAUUSD)")
    s.add_argument("--day", help="date locale New York AAAA-MM-JJ (défaut : aujourd'hui à New York)")
    s.add_argument("--now", help="instant courant ISO 8601 (défaut : horloge système UTC)")
    s.add_argument("--params", help="paramètres clé=valeur (ex. max_hold_min=60,poll_seconds=45)")
    s.add_argument("--out", help="écrire les actions JSON dans ce fichier")
    s.add_argument("--quiet", action="store_true", help="ne pas afficher le JSON")
    s.set_defaults(func=cmd_supervise)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (InputError, ValueError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
