"""Stratégie d'exemple : croisement de deux EMA sur la clôture.

- Entrée ACHAT quand l'EMA rapide passe au-dessus de l'EMA lente ; VENTE
  dans le cas inverse.
- SL = ``atr_mult`` × ATR(``atr_period``) depuis le prix d'entrée (la clôture
  de la bougie de signal), TP = ``rr`` × distance de SL.
- Sortie discrétionnaire sur croisement opposé.
- ``trail_atr_mult`` > 0 active un trailing stop (SL suivi à N × ATR de la
  clôture, uniquement dans le sens favorable) via ``manage``.

Paramètres : fast=9, slow=21, atr_period=14, atr_mult=1.5, rr=2.0, trail_atr_mult=0.0
"""

from __future__ import annotations

from typing import Any

from mt5.engine.indicators import atr, crossover, crossunder, ema
from mt5.engine.strategy import Context, ExitSignal, Signal, Strategy


class EmaCross(Strategy):
    """Croisement EMA rapide / EMA lente avec SL ATR et TP en ratio R."""

    name = "ema_cross"
    default_params: dict[str, Any] = {
        "fast": 9,
        "slow": 21,
        "atr_period": 14,
        "atr_mult": 1.5,
        "rr": 2.0,
        "trail_atr_mult": 0.0,
    }

    @property
    def warmup_bars(self) -> int:
        return max(int(self.params["slow"]), int(self.params["atr_period"])) + 1

    # -- indicateurs mis en cache sur toute la série (causaux) ------------- #
    def _indicators(self, ctx: Context) -> tuple[list, list, list]:
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        period = int(self.params["atr_period"])
        closes = ctx.series("close")
        ema_fast = ctx.cached(("ema", fast), lambda: ema(closes, fast))
        ema_slow = ctx.cached(("ema", slow), lambda: ema(closes, slow))
        atr_values = ctx.cached(
            ("atr", period), lambda: atr(ctx.series("high"), ctx.series("low"), closes, period)
        )
        return ema_fast, ema_slow, atr_values

    def on_bar(self, ctx: Context) -> Signal | None:
        i = ctx.index
        ema_fast, ema_slow, atr_values = self._indicators(ctx)
        current_atr = atr_values[i] if i < len(atr_values) else None
        if current_atr is None or current_atr <= 0:
            return None
        price = ctx.close
        distance = float(self.params["atr_mult"]) * current_atr
        rr = float(self.params["rr"])
        if crossover(ema_fast, ema_slow, i):
            sl = price - distance
            tp = price + rr * distance
            return Signal("buy", sl=sl, tp=tp, reason=f"croisement EMA{self.params['fast']}>EMA{self.params['slow']}")
        if crossunder(ema_fast, ema_slow, i):
            sl = price + distance
            tp = price - rr * distance
            return Signal("sell", sl=sl, tp=tp, reason=f"croisement EMA{self.params['fast']}<EMA{self.params['slow']}")
        return None

    def should_exit(self, ctx: Context) -> ExitSignal | None:
        if ctx.position is None:
            return None
        i = ctx.index
        ema_fast, ema_slow, _ = self._indicators(ctx)
        if ctx.position.direction == "buy" and crossunder(ema_fast, ema_slow, i):
            return ExitSignal("croisement EMA opposé (baissier)")
        if ctx.position.direction == "sell" and crossover(ema_fast, ema_slow, i):
            return ExitSignal("croisement EMA opposé (haussier)")
        return None

    def manage(self, ctx: Context) -> dict[str, float | None] | None:
        trail = float(self.params.get("trail_atr_mult", 0.0) or 0.0)
        if trail <= 0 or ctx.position is None:
            return None
        _, _, atr_values = self._indicators(ctx)
        current_atr = atr_values[ctx.index] if ctx.index < len(atr_values) else None
        if current_atr is None or current_atr <= 0:
            return None
        pos = ctx.position
        if pos.direction == "buy":
            candidate = ctx.close - trail * current_atr
            if pos.sl is None or candidate > pos.sl:
                return {"sl": candidate, "tp": pos.tp}
        else:
            candidate = ctx.close + trail * current_atr
            if pos.sl is None or candidate < pos.sl:
                return {"sl": candidate, "tp": pos.tp}
        return None


STRATEGY = EmaCross
