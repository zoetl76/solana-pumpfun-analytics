"""Modèle de stratégie : spécification du symbole, état de compte/position,
contexte d'évaluation et classe de base ``Strategy``.

Ce module est partagé à l'identique par le backtester (``mt5.engine.backtest``)
et le cycle live (``mt5.engine.cycle``) : une stratégie écrite une fois se
comporte de la même manière dans les deux.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, TypeVar

from .candles import Candle, parse_time

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Spécification du symbole
# --------------------------------------------------------------------------- #
@dataclass
class SymbolSpec:
    """Caractéristiques d'un symbole (format ``get_mt5_available_markets``)."""

    symbol: str = "EURUSD"
    digits: int = 5
    point: float = 0.00001
    contract_size: float = 100_000.0
    currency_base: str = "EUR"
    currency_profit: str = "USD"
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level: int = 1
    freeze_level: int = 0
    trade_mode: str | None = "full"

    @classmethod
    def default_fx(cls, symbol: str = "EURUSD") -> "SymbolSpec":
        """Spécification par défaut d'une paire FX à 5 décimales (type EURUSD)."""
        return cls(symbol=symbol)

    @classmethod
    def from_dict(cls, data: dict[str, Any], symbol: str | None = None) -> "SymbolSpec":
        """Construit la spec depuis le JSON du connecteur (clés manquantes → défauts)."""
        if not isinstance(data, dict):
            raise ValueError("symbol.json : dictionnaire attendu")
        digits = int(_get(data, "digits", default=5))
        point = data.get("point")
        point = float(point) if point else 10.0 ** (-digits)
        return cls(
            symbol=str(symbol or data.get("symbol") or "EURUSD"),
            digits=digits,
            point=point,
            contract_size=float(_get(data, "contractSize", "contract_size", default=100_000)),
            currency_base=str(_get(data, "currencyBase", "currency_base", default="EUR")),
            currency_profit=str(_get(data, "currencyProfit", "currency_profit", default="USD")),
            volume_min=float(_get(data, "volumeMinLots", "volume_min", default=0.01)),
            volume_max=float(_get(data, "volumeMaxLots", "volume_max", default=100.0)),
            volume_step=float(_get(data, "volumeStepLots", "volume_step", default=0.01)),
            stops_level=int(_get(data, "stopsLevel", "stops_level", default=0)),
            freeze_level=int(_get(data, "freezeLevel", "freeze_level", default=0)),
            trade_mode=_norm_mode(data.get("tradeMode", data.get("trade_mode"))),
        )

    def round_price(self, price: float | None) -> float | None:
        """Arrondit un prix au nombre de décimales du symbole."""
        if price is None:
            return None
        return round(float(price), self.digits)

    def to_points(self, price_distance: float) -> float:
        """Convertit une distance en prix en nombre de points."""
        return price_distance / self.point if self.point else 0.0


def _get(data: dict[str, Any], *keys: str, default: Any) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _norm_mode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


# --------------------------------------------------------------------------- #
# Compte et position
# --------------------------------------------------------------------------- #
@dataclass
class AccountState:
    """État du compte (format ``get_account``). ``leverage`` est converti en float."""

    equity: float = 0.0
    balance: float = 0.0
    margin_free: float = 0.0
    margin_used: float = 0.0
    leverage: float = 100.0
    currency: str = "USD"
    as_of: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountState":
        if not isinstance(data, dict):
            raise ValueError("account.json : dictionnaire attendu")
        balance = float(_get(data, "cashBalance", "balance", default=0.0))
        equity = float(_get(data, "equity", default=balance))
        lev_raw = _get(data, "leverage", default=100)
        try:
            leverage = float(str(lev_raw).replace("1:", "").strip() or 100)
        except ValueError:
            leverage = 100.0
        if leverage <= 0:
            leverage = 100.0
        as_of_raw = data.get("asOf") or data.get("time")
        as_of = parse_time(as_of_raw) if as_of_raw else None
        return cls(
            equity=equity,
            balance=balance,
            margin_free=float(_get(data, "marginFree", "margin_free", default=equity)),
            margin_used=float(_get(data, "marginRequirement", "margin", default=0.0)),
            leverage=leverage,
            currency=str(_get(data, "currencyCode", "currency", default="USD")),
            as_of=as_of,
        )


@dataclass
class PositionState:
    """Position ouverte (format ``get_mt5_positions``)."""

    direction: str  # "buy" | "sell"
    volume_lots: float
    price_open: float
    sl: float | None = None
    tp: float | None = None
    time_open: datetime | None = None
    position_id: int | str | None = None
    symbol: str = ""
    price_current: float | None = None
    unrealised_pnl: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PositionState":
        if not isinstance(data, dict):
            raise ValueError("positions.json : entrée non valide (dictionnaire attendu)")
        direction = str(_get(data, "direction", "type", "side", default="")).strip().lower()
        if direction in ("long", "0", "buy"):
            direction = "buy"
        elif direction in ("short", "1", "sell"):
            direction = "sell"
        else:
            raise ValueError(f"Direction de position inconnue : {direction!r}")
        t_raw = _get(data, "timeCreate", "time", "timeOpen", default=None)
        return cls(
            direction=direction,
            volume_lots=float(_get(data, "volumeLots", "volume", default=0.0)),
            price_open=float(_get(data, "priceOpen", "openPrice", default=0.0)),
            sl=_price_or_none(_get(data, "priceSL", "sl", default=None)),
            tp=_price_or_none(_get(data, "priceTP", "tp", default=None)),
            time_open=parse_time(t_raw) if t_raw else None,
            position_id=_get(data, "positionId", "id", "ticket", default=None),
            symbol=str(_get(data, "symbol", default="")),
            price_current=_price_or_none(_get(data, "priceCurrent", default=None)),
            unrealised_pnl=float(_get(data, "unrealisedPnl", "unrealizedPnl", "profit", default=0.0)),
        )


def _price_or_none(value: Any) -> float | None:
    """0, null ou valeur non numérique → ``None`` (SL/TP non défini)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def positions_from_records(records: Any, symbol: str | None = None) -> tuple[list[PositionState], list[str]]:
    """Convertit la liste JSON de positions ; retourne ``(positions, avertissements)``.

    Les entrées illisibles sont ignorées et comptées dans les avertissements.
    Une liste vide (ou ``None``) est parfaitement valide.
    """
    warnings: list[str] = []
    if records is None:
        return [], warnings
    if isinstance(records, dict):
        records = records.get("positions", records.get("data", []))
    if not isinstance(records, list):
        return [], ["positions.json : liste attendue, contenu ignoré"]
    positions: list[PositionState] = []
    bad = 0
    for rec in records:
        try:
            pos = PositionState.from_dict(rec)
        except (ValueError, TypeError):
            bad += 1
            continue
        if symbol is None or pos.symbol.upper() == symbol.upper():
            positions.append(pos)
    if bad:
        warnings.append(f"{bad} position(s) illisible(s) ignorée(s) dans positions.json")
    return positions, warnings


# --------------------------------------------------------------------------- #
# Signaux
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    """Décision d'ENTRÉE prise à la clôture d'une bougie."""

    side: str  # "buy" | "sell"
    sl: float | None = None
    tp: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        self.side = self.side.lower()
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side doit valoir 'buy' ou 'sell', reçu {self.side!r}")


@dataclass
class ExitSignal:
    """Décision de SORTIE discrétionnaire d'une position ouverte."""

    reason: str = ""


# --------------------------------------------------------------------------- #
# Contexte d'évaluation
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    """Tout ce qu'une stratégie peut consulter au moment de décider.

    ``candles`` est la série COMPLÈTE de bougies et ``index`` l'indice de la
    bougie qui vient de clôturer. Une stratégie ne doit JAMAIS lire
    ``candles[index+1:]`` (regard vers le futur en backtest). Les indicateurs
    étant causaux, il est en revanche légitime de les calculer sur toute la
    série et de lire leur valeur à ``index`` ; utilisez ``cached`` pour ne pas
    les recalculer à chaque bougie en backtest.
    """

    candles: list[Candle]
    index: int
    spec: SymbolSpec
    position: PositionState | None = None
    account: AccountState = field(default_factory=AccountState)
    cache: dict[Any, Any] = field(default_factory=dict)

    # -- accès pratiques ---------------------------------------------------- #
    @property
    def candle(self) -> Candle:
        """La bougie courante (celle qui vient de clôturer)."""
        return self.candles[self.index]

    @property
    def time(self) -> datetime:
        """Heure d'ouverture de la bougie courante (UTC)."""
        return self.candle.time

    @property
    def close(self) -> float:
        return self.candle.close

    @property
    def opens(self) -> list[float]:
        """Ouvertures de la série jusqu'à ``index`` inclus."""
        return [c.open for c in self.candles[: self.index + 1]]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles[: self.index + 1]]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles[: self.index + 1]]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles[: self.index + 1]]

    def series(self, name: str) -> list[float]:
        """Série COMPLÈTE (causale) d'un champ de bougie : 'open', 'high', 'low', 'close'…

        À réserver au calcul d'indicateurs (mis en cache) ; n'en lisez que les
        indices ``<= index``.
        """
        key = ("series", name)
        if key not in self.cache:
            self.cache[key] = [getattr(c, name) for c in self.candles]
        return self.cache[key]

    def cached(self, key: Any, factory: Callable[[], T]) -> T:
        """Retourne ``factory()`` mémorisé sous ``key`` pour toute la série."""
        if key not in self.cache:
            self.cache[key] = factory()
        return self.cache[key]

    def has_position(self) -> bool:
        return self.position is not None


# --------------------------------------------------------------------------- #
# Paramètres
# --------------------------------------------------------------------------- #
def parse_params_string(text: str | None) -> dict[str, str]:
    """``"fast=9,slow=21"`` → ``{"fast": "9", "slow": "21"}`` (valeurs brutes)."""
    params: dict[str, str] = {}
    if not text:
        return params
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Paramètre invalide (attendu cle=valeur) : {chunk!r}")
        key, value = chunk.split("=", 1)
        params[key.strip()] = value.strip()
    return params


def coerce_param(value: Any, default: Any) -> Any:
    """Convertit ``value`` vers le type de ``default`` (bool, int, float, str)."""
    if default is None or value is None:
        return value
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "vrai", "oui", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return type(default)(value) if not isinstance(value, type(default)) else value


# --------------------------------------------------------------------------- #
# Classe de base
# --------------------------------------------------------------------------- #
class Strategy:
    """Classe de base d'une stratégie. À sous-classer.

    Attributs de classe à définir :
    - ``name`` : identifiant (= nom du module dans ``mt5/strategies``).
    - ``default_params`` : dictionnaire des paramètres et de leurs défauts ;
      les valeurs passées via ``--params`` sont converties vers le type du défaut.

    Méthodes à surcharger :
    - ``warmup_bars`` : nombre minimal de bougies avant la première décision.
    - ``on_bar(ctx)`` : décision d'entrée (aucune position ouverte).
    - ``should_exit(ctx)`` : sortie discrétionnaire (position ouverte).
    - ``manage(ctx)`` : ajustement SL/TP (trailing) pour la position ouverte.
    """

    name: str = "base"
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged: dict[str, Any] = dict(self.default_params)
        for key, value in (params or {}).items():
            if key in merged:
                merged[key] = coerce_param(value, merged[key])
            else:
                merged[key] = value  # paramètre libre, non typé
        self.params: dict[str, Any] = merged

    # -- API ----------------------------------------------------------------- #
    @property
    def warmup_bars(self) -> int:
        """Nombre de bougies nécessaires avant la première évaluation."""
        return 1

    def on_bar(self, ctx: Context) -> Signal | None:  # noqa: D401 - hook
        """Décision d'entrée à la clôture de ``ctx.candle``. ``None`` = rien."""
        return None

    def should_exit(self, ctx: Context) -> ExitSignal | None:
        """Sortie discrétionnaire de ``ctx.position``. ``None`` = conserver."""
        return None

    def manage(self, ctx: Context) -> dict[str, float | None] | None:
        """Nouveaux niveaux ``{"sl": .., "tp": ..}`` pour la position, ou ``None``."""
        return None

    def describe(self) -> str:
        """Description courte pour les journaux."""
        params = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({params})"


__all__ = [
    "SymbolSpec", "AccountState", "PositionState", "positions_from_records",
    "Signal", "ExitSignal", "Context", "Strategy",
    "parse_params_string", "coerce_param",
]
