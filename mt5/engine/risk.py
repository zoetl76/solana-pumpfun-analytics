"""Gestion du risque : taille de position, marge et garde-fous.

Ce module est utilisé À L'IDENTIQUE par le backtester et par le cycle live :
toute règle ajoutée ici s'applique automatiquement aux deux.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from .candles import parse_time
from .strategy import PositionState, SymbolSpec


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class RiskConfig:
    """Paramètres de risque.

    - ``risk_pct_per_trade`` : % de l'équité risqué entre l'entrée et le SL.
    - ``max_positions_per_symbol`` : nombre maximal de positions ouvertes sur le symbole.
    - ``max_daily_loss_pct`` : perte réalisée du jour (UTC) au-delà de laquelle
      on n'ouvre plus rien (en % du solde).
    - ``max_spread_points`` : spread maximal accepté à l'ouverture (points) ; ``None`` = illimité.
    - ``trading_hours_utc`` : ``(heure_debut, heure_fin)`` UTC autorisées pour
      OUVRIR (fin exclusive ; ``(22, 6)`` couvre la nuit) ; ``None`` = toujours.
    - ``no_new_trades_friday_after_hour_utc`` : plus d'ouverture le vendredi à
      partir de cette heure UTC (``None`` = désactivé).
    - ``margin_usage_cap`` : fraction maximale de la marge libre mobilisable.
    - ``profit_ccy_to_account_rate`` : taux devise de profit → devise du compte
      (1.0 pour EURUSD sur un compte USD).
    """

    risk_pct_per_trade: float = 1.0
    max_positions_per_symbol: int = 1
    max_daily_loss_pct: float = 3.0
    max_spread_points: float | None = None
    trading_hours_utc: tuple[int, int] | None = None
    no_new_trades_friday_after_hour_utc: int | None = 20
    margin_usage_cap: float = 0.8
    profit_ccy_to_account_rate: float = 1.0


@dataclass
class Blocked:
    """Une règle de risque qui empêche l'ouverture d'une position."""

    rule: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "detail": self.detail}


# --------------------------------------------------------------------------- #
# Taille de position
# --------------------------------------------------------------------------- #
@dataclass
class LotSizing:
    """Résultat du calcul de taille : ``lots`` vaut 0 si impossible (voir ``reason``)."""

    lots: float
    risk_money: float = 0.0
    sl_distance: float = 0.0
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.lots > 0


def step_decimals(step: float) -> int:
    """Nombre de décimales du pas de volume (0.01 → 2, 0.1 → 1, 1 → 0)."""
    exponent = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 2


def round_lots_down(lots: float, step: float) -> float:
    """Arrondit ``lots`` VERS LE BAS au multiple de ``step`` (arithmétique entière)."""
    if step <= 0:
        return lots
    steps = math.floor(lots / step + 1e-9)
    return round(steps * step, step_decimals(step))


def compute_lots(
    equity: float,
    risk_pct: float,
    entry_price: float,
    sl_price: float | None,
    symbol_spec: SymbolSpec,
    rate: float = 1.0,
) -> LotSizing:
    """Taille de position pour risquer ``risk_pct`` % de ``equity`` jusqu'au SL.

    lots = argent_risqué / (|entrée - SL| × contractSize × taux), arrondi VERS
    LE BAS au pas de volume puis borné à [volumeMinLots, volumeMaxLots].
    Retourne 0 lot (avec ``reason``) si la distance de SL est nulle/absente ou
    inférieure à ``stopsLevel × point``.
    """
    if sl_price is None:
        return LotSizing(0.0, reason="aucun SL fourni : taille de position impossible")
    distance = abs(float(entry_price) - float(sl_price))
    if distance <= 0.0:
        return LotSizing(0.0, sl_distance=0.0, reason="distance de SL nulle")
    min_distance = symbol_spec.stops_level * symbol_spec.point
    if symbol_spec.stops_level > 0 and distance + 1e-12 < min_distance:
        return LotSizing(
            0.0,
            sl_distance=distance,
            reason=(
                f"distance de SL {distance:.{symbol_spec.digits}f} inférieure au "
                f"stopsLevel ({symbol_spec.stops_level} point(s))"
            ),
        )
    if equity <= 0 or risk_pct <= 0:
        return LotSizing(0.0, sl_distance=distance, reason="équité ou risque nul")
    if rate <= 0:
        return LotSizing(0.0, sl_distance=distance, reason="taux de conversion invalide")

    risk_money = float(equity) * float(risk_pct) / 100.0
    per_lot_loss = distance * symbol_spec.contract_size * rate
    raw = risk_money / per_lot_loss
    lots = round_lots_down(raw, symbol_spec.volume_step)
    warnings: list[str] = []
    if lots < symbol_spec.volume_min:
        lots = symbol_spec.volume_min
        real_risk = lots * per_lot_loss
        warnings.append(
            f"volume minimum {symbol_spec.volume_min} appliqué : risque réel "
            f"{real_risk:.2f} ({real_risk / equity * 100:.2f} % de l'équité)"
        )
    if lots > symbol_spec.volume_max:
        lots = symbol_spec.volume_max
        warnings.append(f"volume plafonné au maximum {symbol_spec.volume_max}")
    return LotSizing(lots=lots, risk_money=risk_money, sl_distance=distance, warnings=warnings)


# --------------------------------------------------------------------------- #
# Marge
# --------------------------------------------------------------------------- #
@dataclass
class MarginCheck:
    ok: bool
    margin_required: float
    margin_allowed: float
    max_lots: float
    detail: str = ""


def coerce_leverage(value: Any, default: float = 100.0) -> float:
    """Convertit le levier du connecteur (``"1000"``, ``"1:500"``, 500) en float > 0."""
    try:
        leverage = float(str(value).replace("1:", "").strip() or default)
    except (TypeError, ValueError):
        leverage = default
    return leverage if leverage > 0 else default


def check_margin(
    lots: float,
    price: float,
    contract_size: float,
    leverage: float,
    margin_free: float,
    cap: float = 0.8,
    volume_step: float = 0.01,
) -> MarginCheck:
    """Vérifie que la marge requise reste sous ``cap × margin_free``.

    APPROXIMATION : marge ≈ lots × contractSize × prix / levier. Cette formule
    est exacte pour un compte libellé dans la devise de COTATION du symbole
    (ex. EURUSD sur un compte USD) et ignore les marges couvertes (hedging), les
    taux de marge spécifiques par symbole et la conversion de devise pour les
    croisements exotiques. Elle est volontairement prudente via ``cap``.
    """
    leverage = coerce_leverage(leverage)
    required = lots * contract_size * price / leverage
    allowed = max(0.0, margin_free) * cap
    per_lot = contract_size * price / leverage
    max_lots = round_lots_down(allowed / per_lot, volume_step) if per_lot > 0 else 0.0
    ok = required <= allowed + 1e-9
    detail = (
        f"marge requise ≈ {required:.2f} pour {lots} lot(s), plafond {allowed:.2f} "
        f"({cap * 100:.0f} % de la marge libre {margin_free:.2f})"
    )
    return MarginCheck(ok=ok, margin_required=required, margin_allowed=allowed, max_lots=max_lots, detail=detail)


# --------------------------------------------------------------------------- #
# Garde-fous (retournent une liste de Blocked, vide si OK)
# --------------------------------------------------------------------------- #
_PROFIT_KEYS = ("profit", "pnl", "realisedPnl", "realizedPnl", "realised_pnl", "realized_pnl")
_TIME_KEYS = ("time", "timeCreate", "dealTime", "timeDone", "closeTime")
_ACTION_KEYS = ("action", "type", "dealType", "entryType")
# Deals qui ne sont PAS du trading : dépôt/retrait (balance), crédit, bonus,
# corrections… Leur ``profit`` est le montant du mouvement et ne doit jamais
# entrer dans le PnL du jour (sinon un dépôt masque les pertes réelles).
_NON_TRADING_MARKERS = ("balance", "credit", "bonus", "correction", "charge", "interest", "dividend")


def is_non_trading_deal(entry: dict[str, Any]) -> bool:
    """Vrai si le deal est une opération de solde (dépôt, retrait, crédit…)."""
    for key in _ACTION_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and any(marker in value.lower() for marker in _NON_TRADING_MARKERS):
            return True
    # Repli : symbole vide ET volume nul (forme des deals « dealbalance » d'Axi).
    if "symbol" in entry and not entry.get("symbol"):
        try:
            if float(entry.get("volumeLots", entry.get("volume", 0)) or 0.0) == 0.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def realized_pnl_today(fills: Any, today: date) -> tuple[float, int, int, int]:
    """Somme du profit réalisé des exécutions de TRADING datées de ``today`` (UTC).

    Analyse défensive : retourne ``(total, nb_pris_en_compte, nb_illisibles,
    nb_operations_de_solde)``. Les entrées sans profit ou sans heure
    exploitables sont ignorées ; les opérations de solde (dépôt, retrait,
    crédit, correction — voir ``is_non_trading_deal``) sont exclues du total.
    """
    if fills is None:
        return 0.0, 0, 0, 0
    if isinstance(fills, dict):
        fills = fills.get("fills", fills.get("deals", fills.get("data", [])))
    if not isinstance(fills, list):
        return 0.0, 0, 1, 0
    total = 0.0
    counted = 0
    ignored = 0
    non_trading = 0
    for entry in fills:
        if not isinstance(entry, dict):
            ignored += 1
            continue
        if is_non_trading_deal(entry):
            non_trading += 1
            continue
        profit: float | None = None
        for key in _PROFIT_KEYS:
            if key in entry and entry[key] is not None:
                try:
                    profit = float(entry[key])
                except (TypeError, ValueError):
                    profit = None
                break
        when: datetime | None = None
        for key in _TIME_KEYS:
            if key in entry and entry[key] is not None:
                try:
                    when = parse_time(entry[key])
                except (TypeError, ValueError):
                    when = None
                break
        if profit is None or when is None:
            ignored += 1
            continue
        if when.date() == today:
            # Ajoute swap/commission s'ils existent (sinon 0).
            extra = 0.0
            for key in ("swap", "commission", "fee"):
                try:
                    extra += float(entry.get(key) or 0.0)
                except (TypeError, ValueError):
                    pass
            total += profit + extra
            counted += 1
    return total, counted, ignored, non_trading


def guard_daily_loss_amount(realized_today: float, reference_balance: float, max_daily_loss_pct: float | None) -> list[Blocked]:
    """Bloque si la perte réalisée du jour atteint ``max_daily_loss_pct`` % du solde."""
    if max_daily_loss_pct is None or max_daily_loss_pct <= 0 or reference_balance <= 0:
        return []
    limit = reference_balance * max_daily_loss_pct / 100.0
    if realized_today <= -limit + 1e-9:
        return [
            Blocked(
                "max_daily_loss",
                f"perte réalisée du jour {realized_today:.2f} ≥ limite {limit:.2f} "
                f"({max_daily_loss_pct} % de {reference_balance:.2f})",
            )
        ]
    return []


def guard_daily_loss(fills: Any, current_balance: float, max_daily_loss_pct: float | None, today: date) -> tuple[list[Blocked], list[str]]:
    """Version « fichier fills.json » : retourne ``(bloqués, avertissements)``.

    ``current_balance`` est le solde ACTUEL du compte (déjà diminué des pertes
    du jour) ; la limite est calculée sur le solde de début de journée,
    reconstitué comme ``current_balance − PnL réalisé du jour``, exactement
    comme le backtester (référence = solde au début du jour).
    """
    total, counted, ignored, non_trading = realized_pnl_today(fills, today)
    warnings: list[str] = []
    if ignored:
        warnings.append(f"{ignored} exécution(s) illisible(s) ignorée(s) dans fills.json")
    if non_trading:
        warnings.append(f"{non_trading} opération(s) de solde (dépôt/retrait/crédit) exclue(s) du PnL du jour")
    if counted:
        warnings.append(f"PnL réalisé du jour ({today.isoformat()}) : {total:.2f} sur {counted} exécution(s)")
    day_start_balance = current_balance - total if counted else current_balance
    return guard_daily_loss_amount(total, day_start_balance, max_daily_loss_pct), warnings


def guard_max_positions(positions: Iterable[PositionState], symbol: str, max_positions: int) -> list[Blocked]:
    """Bloque si le nombre de positions ouvertes sur ``symbol`` atteint le maximum."""
    count = sum(1 for p in positions if not p.symbol or p.symbol.upper() == symbol.upper())
    if count >= max_positions:
        return [Blocked("max_positions", f"{count} position(s) déjà ouverte(s) sur {symbol} (max {max_positions})")]
    return []


def guard_spread(spread_points: float | None, max_spread_points: float | None) -> list[Blocked]:
    """Bloque si le spread courant dépasse le maximum configuré."""
    if max_spread_points is None or spread_points is None:
        return []
    if spread_points > max_spread_points + 1e-9:
        return [Blocked("max_spread", f"spread {spread_points:g} points > maximum {max_spread_points:g}")]
    return []


def in_trading_hours(now: datetime, hours: tuple[int, int] | None) -> bool:
    """Vrai si ``now`` (UTC) est dans la plage ``(debut, fin)`` (fin exclusive, nuit gérée)."""
    if hours is None:
        return True
    start, end = int(hours[0]), int(hours[1])
    hour = now.astimezone(timezone.utc).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # plage de nuit, ex. (22, 6)


def guard_trading_hours(now: datetime, hours: tuple[int, int] | None) -> list[Blocked]:
    if in_trading_hours(now, hours):
        return []
    assert hours is not None
    return [Blocked("trading_hours", f"heure UTC {now.astimezone(timezone.utc).strftime('%H:%M')} hors plage {hours[0]:02d}h-{hours[1]:02d}h")]


def guard_friday_cutoff(now: datetime, cutoff_hour: int | None) -> list[Blocked]:
    """Bloque les ouvertures le vendredi à partir de ``cutoff_hour`` UTC et le week-end.

    ``cutoff_hour=None`` désactive ENTIÈREMENT la règle (vendredi ET week-end),
    ce qui est nécessaire pour les symboles cotés 24h/24 et 7j/7 (crypto CFD).
    """
    if cutoff_hour is None:
        return []
    utc = now.astimezone(timezone.utc)
    weekday = utc.weekday()  # 0 = lundi … 4 = vendredi, 5 = samedi, 6 = dimanche
    if weekday == 5 or (weekday == 6 and utc.hour < 21):
        return [Blocked("weekend", f"marché fermé le week-end ({utc.strftime('%A %H:%M')} UTC)")]
    if weekday == 4 and utc.hour >= cutoff_hour:
        return [Blocked("friday_cutoff", f"vendredi {utc.strftime('%H:%M')} UTC ≥ {cutoff_hour:02d}h : pas de nouvelle position avant le week-end")]
    return []


def validate_stops(
    side: str,
    sl: float | None,
    tp: float | None,
    bid: float,
    ask: float,
    spec: SymbolSpec,
    label: str = "",
) -> list[Blocked]:
    """Vérifie SL/TP comme le ferait le serveur MT5 (règle ``invalid_stops``).

    Un ACHAT se clôture au bid : SL < bid − stopsLevel×point et TP > bid + stopsLevel×point.
    Une VENTE se clôture au ask : SL > ask + stopsLevel×point et TP < ask − stopsLevel×point.
    Vérifier le SL d'un achat contre le ask (prix d'entrée) laisserait passer un
    SL situé entre bid et ask, immédiatement déclenché avec une taille énorme.
    """
    side = side.lower()
    min_dist = max(0, spec.stops_level) * spec.point
    ref = bid if side == "buy" else ask
    ref_name = "bid" if side == "buy" else "ask"
    prefix = f"{label} : " if label else ""
    blocked: list[Blocked] = []
    fmt = f"{{:.{spec.digits}f}}"

    def _far_enough(level: float, below: bool) -> bool:
        gap = (ref - level) if below else (level - ref)
        return gap + 1e-12 >= min_dist and gap > 0

    if sl is not None and not _far_enough(sl, below=(side == "buy")):
        blocked.append(Blocked(
            "invalid_stops",
            f"{prefix}SL {fmt.format(sl)} trop proche ou du mauvais côté du {ref_name} {fmt.format(ref)} "
            f"({side}, stopsLevel {spec.stops_level} point(s))",
        ))
    if tp is not None and not _far_enough(tp, below=(side == "sell")):
        blocked.append(Blocked(
            "invalid_stops",
            f"{prefix}TP {fmt.format(tp)} trop proche ou du mauvais côté du {ref_name} {fmt.format(ref)} "
            f"({side}, stopsLevel {spec.stops_level} point(s))",
        ))
    return blocked


def guard_trade_mode(trade_mode: str | None, direction: str) -> list[Blocked]:
    """Bloque selon le mode du symbole : disabled/closeonly (tout), longonly (sell), shortonly (buy)."""
    if trade_mode is None:
        return []
    mode = trade_mode.lower()
    direction = direction.lower()
    if mode in ("disabled", "closeonly", "close_only"):
        return [Blocked("trade_mode", f"symbole en mode {mode} : ouverture interdite")]
    if mode in ("longonly", "long_only") and direction == "sell":
        return [Blocked("trade_mode", "symbole en mode longonly : vente interdite")]
    if mode in ("shortonly", "short_only") and direction == "buy":
        return [Blocked("trade_mode", "symbole en mode shortonly : achat interdit")]
    return []


def guard_session(trade_session_open: bool | None, opens_at: str | None = None) -> list[Blocked]:
    """Bloque si la cotation indique une session de trading fermée."""
    if trade_session_open is False:
        detail = "session de trading fermée"
        if opens_at:
            detail += f" (réouverture {opens_at})"
        return [Blocked("session_closed", detail)]
    return []


__all__ = [
    "RiskConfig", "Blocked", "LotSizing", "MarginCheck",
    "step_decimals", "round_lots_down", "compute_lots", "coerce_leverage", "check_margin",
    "is_non_trading_deal", "realized_pnl_today", "guard_daily_loss_amount", "guard_daily_loss",
    "validate_stops",
    "guard_max_positions", "guard_spread", "in_trading_hours", "guard_trading_hours",
    "guard_friday_cutoff", "guard_trade_mode", "guard_session",
]
