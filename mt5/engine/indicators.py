"""Indicateurs techniques en pur Python.

Toutes les fonctions prennent des listes de ``float`` et retournent des listes
ALIGNÉES sur l'entrée : la valeur à l'indice ``i`` ne dépend que des entrées
``0..i`` (aucun regard vers le futur). Pendant la période de chauffe
(« warm-up ») les valeurs valent ``None``.

Les valeurs ``None`` en entrée sont tolérées : elles propagent ``None`` en
sortie pour l'indice concerné et remettent le calcul à zéro (rare en pratique).
"""

from __future__ import annotations

import math
from typing import Sequence

Series = Sequence[float | None]
Out = list[float | None]


def _valid(values: Series) -> list[float]:
    """Retourne la liste avec les ``None`` remplacés par ``nan`` (calculs internes)."""
    return [float("nan") if v is None else float(v) for v in values]


def _clean(values: list[float]) -> Out:
    """Convertit les ``nan`` en ``None`` pour la sortie."""
    return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else v for v in values]


def sma(values: Series, period: int) -> Out:
    """Moyenne mobile simple sur ``period`` valeurs."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    data = _valid(values)
    out: list[float] = [float("nan")] * len(data)
    window_sum = 0.0
    nan_in_window = 0  # nombre de valeurs manquantes dans la fenêtre courante
    for i, v in enumerate(data):
        if math.isnan(v):
            nan_in_window += 1
        else:
            window_sum += v
        if i >= period:
            leaving = data[i - period]
            if math.isnan(leaving):
                nan_in_window -= 1
            else:
                window_sum -= leaving
        if i >= period - 1 and nan_in_window == 0:
            out[i] = window_sum / period
    return _clean(out)


def ema(values: Series, period: int) -> Out:
    """Moyenne mobile exponentielle (amorcée par la SMA des ``period`` premières valeurs)."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    data = _valid(values)
    out: list[float] = [float("nan")] * len(data)
    alpha = 2.0 / (period + 1.0)
    prev = float("nan")
    for i, v in enumerate(data):
        if math.isnan(v):
            prev = float("nan")
            continue
        if math.isnan(prev):
            if i >= period - 1:
                window = data[i - period + 1 : i + 1]
                if any(math.isnan(x) for x in window):
                    continue
                prev = sum(window) / period
                out[i] = prev
            continue
        prev = alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return _clean(out)


def rsi(values: Series, period: int = 14) -> Out:
    """RSI de Wilder (lissage exponentiel des gains/pertes moyens)."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    data = _valid(values)
    n = len(data)
    out: list[float] = [float("nan")] * n
    if n < period + 1:
        return _clean(out)
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, n):
        if math.isnan(data[i]) or math.isnan(data[i - 1]):
            continue
        change = data[i] - data[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if i <= period:
            avg_gain += gain / period
            avg_loss += loss / period
            if i == period:
                out[i] = _rsi_value(avg_gain, avg_loss)
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            out[i] = _rsi_value(avg_gain, avg_loss)
    return _clean(out)


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(highs: Series, lows: Series, closes: Series) -> Out:
    """True Range : TR[0] = high-low ; TR[i] = max(h-l, |h-pc|, |l-pc|)."""
    h, l, c = _valid(highs), _valid(lows), _valid(closes)
    out: list[float] = []
    for i in range(len(h)):
        if i == 0 or math.isnan(c[i - 1]):
            out.append(h[i] - l[i])
        else:
            out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return _clean(out)


def atr(highs: Series, lows: Series, closes: Series, period: int = 14) -> Out:
    """ATR de Wilder.

    Première valeur à l'indice ``period-1`` = moyenne simple des ``period``
    premiers True Range, puis ATR[i] = (ATR[i-1]*(period-1) + TR[i]) / period.
    """
    if period <= 0:
        raise ValueError("period doit être > 0")
    tr = _valid(true_range(highs, lows, closes))
    out: list[float] = [float("nan")] * len(tr)
    prev = float("nan")
    for i, v in enumerate(tr):
        if math.isnan(v):
            prev = float("nan")
            continue
        if math.isnan(prev):
            if i >= period - 1:
                window = tr[i - period + 1 : i + 1]
                if any(math.isnan(x) for x in window):
                    continue
                prev = sum(window) / period
                out[i] = prev
            continue
        prev = (prev * (period - 1) + v) / period
        out[i] = prev
    return _clean(out)


def stddev(values: Series, period: int) -> Out:
    """Écart-type (population, comme MetaTrader) glissant sur ``period`` valeurs."""
    data = _valid(values)
    out: list[float] = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1 : i + 1]
        if any(math.isnan(x) for x in window):
            continue
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        out[i] = math.sqrt(var)
    return _clean(out)


def bollinger(values: Series, period: int = 20, k: float = 2.0) -> tuple[Out, Out, Out]:
    """Bandes de Bollinger : retourne ``(milieu, haute, basse)``."""
    mid = sma(values, period)
    sd = stddev(values, period)
    upper: Out = []
    lower: Out = []
    for m, s in zip(mid, sd):
        if m is None or s is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(m + k * s)
            lower.append(m - k * s)
    return mid, upper, lower


def macd(values: Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Out, Out, Out]:
    """MACD : retourne ``(macd, signal, histogramme)``."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: Out = [
        None if (a is None or b is None) else a - b for a, b in zip(ema_fast, ema_slow)
    ]
    sig = ema(line, signal)
    hist: Out = [None if (m is None or s is None) else m - s for m, s in zip(line, sig)]
    return line, sig, hist


def highest(values: Series, period: int) -> Out:
    """Plus haut glissant sur ``period`` valeurs (incluant la valeur courante)."""
    data = _valid(values)
    out: list[float] = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1 : i + 1]
        if any(math.isnan(x) for x in window):
            continue
        out[i] = max(window)
    return _clean(out)


def lowest(values: Series, period: int) -> Out:
    """Plus bas glissant sur ``period`` valeurs (incluant la valeur courante)."""
    data = _valid(values)
    out: list[float] = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1 : i + 1]
        if any(math.isnan(x) for x in window):
            continue
        out[i] = min(window)
    return _clean(out)


def crossover(a: Series, b: Series, i: int) -> bool:
    """Vrai si ``a`` passe AU-DESSUS de ``b`` à l'indice ``i`` (a[i-1] <= b[i-1] et a[i] > b[i])."""
    if i <= 0 or i >= len(a) or i >= len(b):
        return False
    a0, b0, a1, b1 = a[i - 1], b[i - 1], a[i], b[i]
    if a0 is None or b0 is None or a1 is None or b1 is None:
        return False
    return a0 <= b0 and a1 > b1


def crossunder(a: Series, b: Series, i: int) -> bool:
    """Vrai si ``a`` passe EN DESSOUS de ``b`` à l'indice ``i`` (a[i-1] >= b[i-1] et a[i] < b[i])."""
    if i <= 0 or i >= len(a) or i >= len(b):
        return False
    a0, b0, a1, b1 = a[i - 1], b[i - 1], a[i], b[i]
    if a0 is None or b0 is None or a1 is None or b1 is None:
        return False
    return a0 >= b0 and a1 < b1


__all__ = [
    "sma", "ema", "rsi", "true_range", "atr", "stddev", "bollinger", "macd",
    "highest", "lowest", "crossover", "crossunder",
]
