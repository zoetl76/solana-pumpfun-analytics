"""Stratégie « NY Open 9:31 » sur l'or (XAUUSD) — module de décision pur.

Règles (voir mt5/STRATEGIE_NY_OPEN.md) :

- La bougie de référence est la bougie M1 qui ouvre à 9h31 heure de New York
  (fuseau ``America/New_York``, changement d'heure géré automatiquement).
- À sa clôture (9h32) on place deux ordres stop : un achat au-dessus du plus
  haut (décalage + spread, car un buy stop se déclenche sur le ask) et une vente
  sous le plus bas (décalage seul, un sell stop se déclenche sur le bid).
- Le stop de protection est à l'autre extrémité de la bougie de référence ;
  l'objectif vaut ``rr`` fois la distance de risque.
- Les ordres expirent ``entry_window_min`` minutes après 9h32 (côté broker).
- Une position encore ouverte ``max_hold_min`` minutes après son ouverture est
  fermée au marché.
- Un seul trade par jour : dès qu'un ordre est déclenché l'autre est annulé ;
  si le second part quand même (latence), la seconde position est fermée.
- Taille : ``risk_pct`` % de l'équité entre l'entrée et le stop.

Ce module ne parle jamais au réseau : il calcule un *plan* (les deux ordres) et
des *actions de supervision* (annuler, fermer) à partir de JSON du connecteur.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from mt5.engine.candles import Candle, parse_time
from mt5.engine.risk import compute_lots
from mt5.engine.strategy import PositionState, SymbolSpec, coerce_param

NAME = "ny_open_931"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class NyOpenConfig:
    """Paramètres de la stratégie (tous surchargeables via ``--params``)."""

    symbol: str = "XAUUSD"
    timezone: str = "America/New_York"
    ref_hour: int = 9
    ref_minute: int = 31
    buffer_buy: float = 0.10  # USD au-dessus du plus haut (le spread s'ajoute)
    buffer_sell: float = 0.10  # USD sous le plus bas
    add_spread_to_buy: bool = True
    rr: float = 2.0
    risk_pct: float = 0.5
    entry_window_min: int = 30
    max_hold_min: int = 60
    close_second_position: bool = True
    skip_if_late: bool = True
    min_range: float = 0.0  # 0 = désactivé
    max_range: float = 0.0  # 0 = désactivé
    poll_seconds: int = 45
    skip_dates: str = ""  # "2026-11-26,2026-12-25"

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> "NyOpenConfig":
        """Construit la config depuis un dict ``clé → valeur`` (types déduits des défauts)."""
        cfg = cls()
        for key, value in (params or {}).items():
            if not hasattr(cfg, key):
                raise ValueError(f"Paramètre inconnu pour {NAME} : {key!r}")
            default = getattr(cfg, key)
            setattr(cfg, key, coerce_param(value, default))
        if cfg.rr <= 0:
            raise ValueError("rr doit être > 0")
        if cfg.entry_window_min <= 0 or cfg.max_hold_min <= 0:
            raise ValueError("entry_window_min et max_hold_min doivent être > 0")
        if cfg.buffer_buy < 0 or cfg.buffer_sell < 0:
            raise ValueError("les décalages (buffer) ne peuvent pas être négatifs")
        return cfg

    def skip_date_set(self) -> set[date]:
        out: set[date] = set()
        for chunk in (self.skip_dates or "").split(","):
            chunk = chunk.strip()
            if chunk:
                out.add(date.fromisoformat(chunk))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --------------------------------------------------------------------------- #
# Horaires
# --------------------------------------------------------------------------- #
def _tz(cfg: NyOpenConfig) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def ref_bar_start(day: date, cfg: NyOpenConfig) -> datetime:
    """Heure d'OUVERTURE (UTC) de la bougie de référence du jour ``day`` (date locale)."""
    local = datetime(day.year, day.month, day.day, cfg.ref_hour, cfg.ref_minute, tzinfo=_tz(cfg))
    return local.astimezone(timezone.utc)


def ref_bar_close(day: date, cfg: NyOpenConfig) -> datetime:
    """Clôture de la bougie de référence = moment où l'on place les ordres."""
    return ref_bar_start(day, cfg) + timedelta(minutes=1)


def orders_expiration(day: date, cfg: NyOpenConfig) -> datetime:
    """Expiration des ordres en attente (UTC)."""
    return ref_bar_close(day, cfg) + timedelta(minutes=cfg.entry_window_min)


def hard_deadline(day: date, cfg: NyOpenConfig) -> datetime:
    """Au plus tard à cet instant, plus aucune position du jour ne doit exister."""
    return orders_expiration(day, cfg) + timedelta(minutes=cfg.max_hold_min)


def local_day(moment: datetime, cfg: NyOpenConfig) -> date:
    """Date locale (New York) d'un instant UTC-aware."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_tz(cfg)).date()


def find_ref_bar(candles: list[Candle], day: date, cfg: NyOpenConfig) -> Candle | None:
    """Retourne la bougie qui ouvre exactement à l'heure de référence, sinon ``None``."""
    target = ref_bar_start(day, cfg)
    for candle in candles:
        if candle.time == target:
            return candle
    return None


# --------------------------------------------------------------------------- #
# Plan des ordres
# --------------------------------------------------------------------------- #
@dataclass
class PendingOrder:
    """Un ordre stop prêt à être passé via ``place_mt5_order``."""

    type: str  # "buy_stop" | "sell_stop"
    price: float
    sl: float
    tp: float
    lots: float
    risk_money: float
    risk_distance: float
    expiration: datetime
    warnings: list[str] = field(default_factory=list)

    @property
    def side(self) -> str:
        return "buy" if self.type.startswith("buy") else "sell"

    def to_connector_args(self, symbol: str) -> dict[str, Any]:
        """Arguments exacts de l'outil ``place_mt5_order`` (hors accountId)."""
        return {
            "symbol": symbol,
            "type": self.type,
            "volumeLots": self.lots,
            "priceOrder": self.price,
            "priceSL": self.sl,
            "priceTP": self.tp,
            "timeType": "specified",
            "expiration": self.expiration.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def to_dict(self, symbol: str) -> dict[str, Any]:
        data = self.to_connector_args(symbol)
        data.update({
            "riskMoney": round(self.risk_money, 2),
            "riskDistance": self.risk_distance,
            "warnings": list(self.warnings),
        })
        return data


@dataclass
class Plan:
    """Résultat de la planification du jour."""

    day: date
    ref_bar: Candle | None
    high: float
    low: float
    range: float
    spread: float
    equity: float
    orders: list[PendingOrder]
    expiration: datetime
    deadline: datetime
    skipped: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.skipped is None and len(self.orders) == 2

    def to_dict(self, symbol: str) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "refBar": self.ref_bar.to_dict() if self.ref_bar else None,
            "high": self.high,
            "low": self.low,
            "range": round(self.range, 5),
            "spread": self.spread,
            "equity": self.equity,
            "orders": [o.to_dict(symbol) for o in self.orders],
            "expiration": _iso(self.expiration),
            "hardDeadline": _iso(self.deadline),
            "skipped": self.skipped,
            "warnings": list(self.warnings),
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def plan(
    ref_bar: Candle,
    spread: float,
    equity: float,
    spec: SymbolSpec,
    cfg: NyOpenConfig,
    bid: float | None = None,
    ask: float | None = None,
    now: datetime | None = None,
) -> Plan:
    """Calcule les deux ordres stop du jour à partir de la bougie de référence.

    ``spread`` est en PRIX (ask − bid), pas en points. ``bid``/``ask`` sont la
    cotation au moment de la planification : si le prix a déjà dépassé un niveau
    (ordres placés en retard) le jour est sauté quand ``skip_if_late`` est vrai,
    car un stop déjà franchi serait refusé par MT5 ou exécuté au marché.
    """
    day = local_day(ref_bar.time, cfg)
    expiration = orders_expiration(day, cfg)
    deadline = hard_deadline(day, cfg)
    high, low = float(ref_bar.high), float(ref_bar.low)
    rng = high - low
    spread = max(0.0, float(spread or 0.0))
    base = Plan(day, ref_bar, high, low, rng, spread, float(equity), [], expiration, deadline)

    def skip(reason: str) -> Plan:
        base.skipped = reason
        return base

    expected = ref_bar_start(day, cfg)
    if ref_bar.time != expected:
        return skip(
            f"bougie de référence incorrecte : ouvre à {_iso(ref_bar.time)}, attendu {_iso(expected)}"
        )
    if day in cfg.skip_date_set():
        return skip(f"date exclue par configuration ({day.isoformat()})")
    if day.weekday() >= 5:
        return skip("week-end : pas de séance")
    if rng <= 0:
        return skip("bougie de référence sans amplitude (high == low)")
    if cfg.min_range > 0 and rng < cfg.min_range:
        return skip(f"amplitude {rng:.{spec.digits}f} sous le minimum {cfg.min_range}")
    if cfg.max_range > 0 and rng > cfg.max_range:
        return skip(f"amplitude {rng:.{spec.digits}f} au-dessus du maximum {cfg.max_range}")
    if now is not None and now >= expiration:
        return skip(f"trop tard : la fenêtre d'entrée est close depuis {_iso(expiration)}")

    buy_price = spec.round_price(high + cfg.buffer_buy + (spread if cfg.add_spread_to_buy else 0.0))
    sell_price = spec.round_price(low - cfg.buffer_sell)
    assert buy_price is not None and sell_price is not None
    buy_sl, sell_sl = spec.round_price(low), spec.round_price(high)
    assert buy_sl is not None and sell_sl is not None
    buy_tp = spec.round_price(buy_price + cfg.rr * (buy_price - buy_sl))
    sell_tp = spec.round_price(sell_price - cfg.rr * (sell_sl - sell_price))
    assert buy_tp is not None and sell_tp is not None

    min_gap = max(0, spec.stops_level) * spec.point
    if cfg.skip_if_late and ask is not None and ask + min_gap > buy_price + 1e-12:
        return skip(
            f"retard : le ask {ask:.{spec.digits}f} a déjà atteint le niveau d'achat {buy_price:.{spec.digits}f}"
        )
    if cfg.skip_if_late and bid is not None and bid - min_gap < sell_price - 1e-12:
        return skip(
            f"retard : le bid {bid:.{spec.digits}f} a déjà atteint le niveau de vente {sell_price:.{spec.digits}f}"
        )

    orders: list[PendingOrder] = []
    for otype, price, sl, tp in (
        ("buy_stop", buy_price, buy_sl, buy_tp),
        ("sell_stop", sell_price, sell_sl, sell_tp),
    ):
        sizing = compute_lots(equity, cfg.risk_pct, price, sl, spec)
        if not sizing.ok:
            return skip(f"taille impossible pour {otype} : {sizing.reason}")
        orders.append(PendingOrder(
            type=otype, price=price, sl=sl, tp=tp, lots=sizing.lots,
            risk_money=sizing.lots * sizing.sl_distance * spec.contract_size,
            risk_distance=round(sizing.sl_distance, spec.digits),
            expiration=expiration, warnings=list(sizing.warnings),
        ))
    base.orders = orders
    if spread > 0 and spread / rng > 0.15:
        base.warnings.append(
            f"spread {spread:.{spec.digits}f} = {spread / rng * 100:.0f} % de l'amplitude : coût élevé"
        )
    return base


# --------------------------------------------------------------------------- #
# Supervision (après placement des ordres)
# --------------------------------------------------------------------------- #
@dataclass
class PendingOrderState:
    """Ordre en attente tel que renvoyé par ``get_mt5_orders``."""

    order_id: int | str
    symbol: str
    type: str
    price: float | None = None
    time_setup: datetime | None = None
    expiration: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingOrderState":
        if not isinstance(data, dict):
            raise ValueError("orders.json : entrée non valide (dictionnaire attendu)")
        oid = data.get("orderId", data.get("id", data.get("ticket")))
        if oid is None:
            raise ValueError("orders.json : orderId manquant")
        t_raw = data.get("timeSetup") or data.get("time")
        e_raw = data.get("timeExpiration")
        price = data.get("priceOrder", data.get("price"))
        return cls(
            order_id=oid,
            symbol=str(data.get("symbol", "")),
            type=str(data.get("type", "")).lower(),
            price=float(price) if price not in (None, "") else None,
            time_setup=parse_time(t_raw) if t_raw else None,
            expiration=parse_time(e_raw) if e_raw else None,
        )


@dataclass
class Action:
    type: str  # "cancel_order" | "close_position"
    reason: str
    order_id: int | str | None = None
    position_id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "reason": self.reason}
        if self.order_id is not None:
            data["orderId"] = self.order_id
        if self.position_id is not None:
            data["positionId"] = self.position_id
        return data


@dataclass
class Supervision:
    now: datetime
    actions: list[Action]
    done: bool
    next_check_seconds: int
    state: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": _iso(self.now),
            "actions": [a.to_dict() for a in self.actions],
            "done": self.done,
            "nextCheckSeconds": self.next_check_seconds,
            "state": self.state,
            "warnings": list(self.warnings),
        }


def _same_symbol(a: str, b: str) -> bool:
    return a.strip().upper() == b.strip().upper()


def supervise(
    now: datetime,
    positions: list[PositionState],
    orders: list[PendingOrderState],
    day: date,
    cfg: NyOpenConfig,
    trade_done: bool = False,
) -> Supervision:
    """Décide les actions de supervision pour l'instant ``now`` (UTC-aware).

    - une position ouverte → annuler tout ordre en attente restant (OCO manuel),
      fermer toute position supplémentaire, fermer la position au bout de
      ``max_hold_min`` minutes ;
    - aucune position et fenêtre d'entrée close → annuler les ordres restants
      (sécurité : ils devraient avoir expiré côté broker) ;
    - ``trade_done`` (déduit des exécutions du jour) : le trade du jour est
      déjà clos, donc rien ne doit être ré-ouvert et la journée est finie.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = ref_bar_start(day, cfg)
    expiration = orders_expiration(day, cfg)
    deadline = hard_deadline(day, cfg)
    warnings: list[str] = []

    ours_pos = [
        p for p in positions
        if _same_symbol(p.symbol or cfg.symbol, cfg.symbol) and (p.time_open is None or p.time_open >= start)
    ]
    foreign_pos = [p for p in positions if p not in ours_pos]
    if foreign_pos:
        warnings.append(f"{len(foreign_pos)} position(s) hors périmètre ignorée(s) (autre symbole ou antérieure)")
    ours_ord = [
        o for o in orders
        if _same_symbol(o.symbol or cfg.symbol, cfg.symbol) and (o.time_setup is None or o.time_setup >= start)
    ]
    foreign_ord = [o for o in orders if o not in ours_ord]
    if foreign_ord:
        warnings.append(f"{len(foreign_ord)} ordre(s) hors périmètre ignoré(s)")

    actions: list[Action] = []
    state: dict[str, Any] = {
        "day": day.isoformat(),
        "refBarStart": _iso(start),
        "ordersExpiration": _iso(expiration),
        "hardDeadline": _iso(deadline),
        "positions": len(ours_pos),
        "pendingOrders": len(ours_ord),
        "tradeDone": bool(trade_done),
    }

    def _sort_key(p: PositionState) -> tuple[int, datetime]:
        return (0, p.time_open) if p.time_open else (1, now)

    if ours_pos:
        ours_pos = sorted(ours_pos, key=_sort_key)
        primary, extras = ours_pos[0], ours_pos[1:]
        for o in ours_ord:
            actions.append(Action("cancel_order", "OCO : une position est ouverte, l'ordre opposé est annulé", order_id=o.order_id))
        for p in extras:
            if cfg.close_second_position:
                actions.append(Action(
                    "close_position",
                    "seconde position du jour (un seul trade par jour) : fermeture immédiate",
                    position_id=p.position_id,
                ))
            else:
                warnings.append(f"position supplémentaire {p.position_id} laissée ouverte (close_second_position=false)")
        opened = primary.time_open or now
        time_stop = opened + timedelta(minutes=cfg.max_hold_min)
        state["primary"] = {
            "positionId": primary.position_id,
            "direction": primary.direction,
            "volumeLots": primary.volume_lots,
            "priceOpen": primary.price_open,
            "sl": primary.sl,
            "tp": primary.tp,
            "timeOpen": _iso(opened),
            "timeStop": _iso(time_stop),
        }
        if now >= time_stop:
            actions.append(Action(
                "close_position",
                f"limite de temps : position ouverte depuis {cfg.max_hold_min} min",
                position_id=primary.position_id,
            ))
        elif now >= deadline:
            actions.append(Action("close_position", "échéance du jour dépassée : fermeture de sécurité", position_id=primary.position_id))
        if primary.sl is None or primary.tp is None:
            warnings.append("la position n'a pas de SL ou de TP côté broker : à corriger immédiatement")
        remaining = int((time_stop - now).total_seconds())
        next_check = max(5, min(cfg.poll_seconds, remaining)) if remaining > 0 else 5
        return Supervision(now, actions, False, next_check, state, warnings)

    # Aucune position du jour.
    if ours_ord:
        if now >= expiration or trade_done:
            reason = "fenêtre d'entrée close : annulation de sécurité" if now >= expiration else "trade du jour déjà réalisé : aucun second essai"
            for o in ours_ord:
                actions.append(Action("cancel_order", reason, order_id=o.order_id))
            return Supervision(now, actions, False, 5, state, warnings)
        remaining = int((expiration - now).total_seconds())
        next_check = max(5, min(cfg.poll_seconds, remaining + 5))
        return Supervision(now, actions, False, next_check, state, warnings)

    if trade_done or now >= expiration:
        return Supervision(now, actions, True, 0, state, warnings)
    warnings.append("aucun ordre en attente ni position avant l'expiration : ordres non placés ?")
    return Supervision(now, actions, False, cfg.poll_seconds, state, warnings)


def trade_done_from_fills(fills: Any, day: date, cfg: NyOpenConfig) -> bool:
    """Vrai si une exécution de SORTIE sur le symbole a eu lieu depuis la bougie de référence."""
    if not isinstance(fills, list):
        return False
    start = ref_bar_start(day, cfg)
    for entry in fills:
        if not isinstance(entry, dict):
            continue
        if not _same_symbol(str(entry.get("symbol", "")), cfg.symbol):
            continue
        entry_kind = str(entry.get("entry", "")).lower()
        if "out" not in entry_kind:
            continue
        t_raw = entry.get("time")
        try:
            when = parse_time(t_raw) if t_raw else None
        except (TypeError, ValueError):
            when = None
        if when is not None and when >= start:
            return True
    return False


__all__ = [
    "NAME", "NyOpenConfig", "PendingOrder", "Plan", "PendingOrderState", "Action", "Supervision",
    "ref_bar_start", "ref_bar_close", "orders_expiration", "hard_deadline", "local_day", "find_ref_bar",
    "plan", "supervise", "trade_done_from_fills",
]
