"""Chargement et validation des bougies (format Axi MT5).

Format d'entrée (liste JSON, plus ancienne bougie en premier) ::

    {"time": "2026-09-24T00:00:00+00:00", "open": 1.13815, "high": 1.13847,
     "low": 1.13707, "close": 1.13763, "tickVolume": 2594, "volume": 0,
     "spread": 6}

- ``time`` est une date ISO 8601 (suffixe ``+00:00`` ou ``Z``) ; une date
  « naïve » (sans fuseau) est interprétée comme UTC ; un nombre est interprété
  comme un timestamp Unix en secondes.
- ``spread`` est exprimé en POINTS (et non en unités de prix).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Candle:
    """Une bougie terminée. ``time`` est l'heure d'OUVERTURE, UTC-aware."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    volume: float = 0.0
    spread_points: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Retourne la bougie au format JSON Axi (clés camelCase)."""
        return {
            "time": self.time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "tickVolume": self.tick_volume,
            "volume": self.volume,
            "spread": self.spread_points,
        }


def parse_time(value: Any) -> datetime:
    """Convertit une valeur JSON en ``datetime`` UTC-aware.

    Accepte : ISO 8601 avec ``+00:00`` ou ``Z``, ISO sans fuseau (supposé UTC),
    ou un nombre (timestamp Unix en secondes, ou millisecondes si > 1e12).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # millisecondes
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:  # pragma: no cover - message d'erreur seulement
            raise ValueError(f"Horodatage invalide : {value!r}") from exc
    else:
        raise ValueError(f"Horodatage de type non supporté : {type(value).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_float(record: dict[str, Any], *keys: str, default: float | None = None) -> float:
    for key in keys:
        if key in record and record[key] is not None:
            return float(record[key])
    if default is None:
        raise ValueError(f"Champ manquant : {keys[0]!r} dans {record!r}")
    return default


def candle_from_dict(record: dict[str, Any]) -> Candle:
    """Construit une ``Candle`` à partir d'un dictionnaire JSON Axi (tolérant)."""
    if not isinstance(record, dict):
        raise ValueError(f"Bougie invalide (dictionnaire attendu) : {record!r}")
    if "time" not in record:
        raise ValueError(f"Bougie sans champ 'time' : {record!r}")
    open_ = _to_float(record, "open", "o")
    high = _to_float(record, "high", "h")
    low = _to_float(record, "low", "l")
    close = _to_float(record, "close", "c")
    if low > high + 1e-12:
        raise ValueError(f"Bougie incohérente (low > high) : {record!r}")
    if not (low - 1e-12 <= open_ <= high + 1e-12) or not (low - 1e-12 <= close <= high + 1e-12):
        raise ValueError(f"Bougie incohérente (open/close hors de [low, high]) : {record!r}")
    return Candle(
        time=parse_time(record["time"]),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=int(_to_float(record, "tickVolume", "tick_volume", default=0.0)),
        volume=_to_float(record, "volume", "realVolume", default=0.0),
        spread_points=int(round(_to_float(record, "spread", "spread_points", default=0.0))),
    )


def candles_from_records(records: Iterable[dict[str, Any]]) -> list[Candle]:
    """Convertit, trie (plus ancienne en premier) et dédoublonne des bougies.

    Si deux bougies portent le même horodatage, la DERNIÈRE rencontrée est
    conservée (elle est supposée la plus à jour).
    """
    parsed = [candle_from_dict(r) for r in records]
    # Tri stable par temps : valide l'ordre « plus ancienne en premier ».
    parsed.sort(key=lambda c: c.time)
    deduped: dict[datetime, Candle] = {}
    for candle in parsed:
        deduped[candle.time] = candle
    return [deduped[t] for t in sorted(deduped)]


def _extract_list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    """Accepte une liste brute ou un dictionnaire enveloppant une liste."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError("Liste JSON attendue")


def load_candles(path: str | Path) -> list[Candle]:
    """Charge un fichier JSON de bougies Axi et retourne une liste validée.

    Lève ``ValueError`` si le fichier n'est pas une liste de bougies valides.
    Une liste vide est acceptée (retourne ``[]``).
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    records = _extract_list(payload, ("candles", "data", "bars"))
    return candles_from_records(records)


def candles_to_json(candles: Iterable[Candle]) -> list[dict[str, Any]]:
    """Sérialise des bougies au format Axi (utile pour les tests)."""
    return [c.to_dict() for c in candles]


def save_candles(candles: Iterable[Candle], path: str | Path) -> None:
    """Écrit des bougies au format Axi dans un fichier JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(candles_to_json(candles), fh, indent=2)


def infer_timeframe(candles: list[Candle]) -> timedelta | None:
    """Déduit la durée d'une bougie (médiane des écarts entre bougies).

    Retourne ``None`` s'il y a moins de deux bougies.
    """
    if len(candles) < 2:
        return None
    deltas = [
        (candles[i].time - candles[i - 1].time).total_seconds()
        for i in range(1, len(candles))
        if candles[i].time > candles[i - 1].time
    ]
    if not deltas:
        return None
    return timedelta(seconds=statistics.median(deltas))


def candle_close_time(candles: list[Candle], index: int = -1) -> datetime:
    """Heure de CLÔTURE d'une bougie = ouverture + durée déduite (sinon ouverture)."""
    tf = infer_timeframe(candles)
    candle = candles[index]
    return candle.time + tf if tf else candle.time


__all__ = [
    "Candle",
    "parse_time",
    "candle_from_dict",
    "candles_from_records",
    "load_candles",
    "candles_to_json",
    "save_candles",
    "infer_timeframe",
    "candle_close_time",
    "asdict",
]
