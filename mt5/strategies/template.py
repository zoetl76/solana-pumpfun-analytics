"""GABARIT DE STRATÉGIE — copiez ce fichier pour écrire la vôtre.

Mode d'emploi rapide
--------------------
1. Copiez ``mt5/strategies/template.py`` vers ``mt5/strategies/ma_strategie.py``.
2. Renommez la classe et mettez ``name = "ma_strategie"`` (= nom du fichier).
3. Remplissez ``default_params``, ``warmup_bars``, ``on_bar`` et, si besoin,
   ``should_exit`` et ``manage``.
4. Testez en backtest ::

       python3 -m mt5.engine.backtest --strategy ma_strategie \
           --candles mt5/data/EURUSD_H1_sample.json --symbol EURUSD --balance 10000

5. Utilisez-la en live (via Claude + connecteur Axi) ::

       python3 -m mt5.engine.cycle --strategy ma_strategie --symbol EURUSD ...

Règles d'or
-----------
- La stratégie est SANS ÉTAT : ne stockez rien entre deux appels (en live,
  chaque cycle repart d'un processus neuf). Tout ce qu'il faut savoir se
  déduit de ``ctx`` (bougies, position ouverte, compte).
- Ne lisez JAMAIS ``ctx.candles[ctx.index + 1:]`` : c'est le futur.
- Toujours fournir un SL dans le ``Signal`` : c'est lui qui sert à calculer
  la taille de position (risque en % de l'équité). Sans SL, aucun trade.
- Les prix retournés (SL/TP) n'ont pas besoin d'être arrondis : le moteur
  les arrondit aux décimales du symbole.

Ce gabarit est enregistré sous le nom ``template`` mais ne prend AUCUNE
position (``on_bar`` retourne ``None``) : il sert de point de départ.
"""

from __future__ import annotations

from typing import Any

# Indicateurs disponibles (tous alignés sur la série, None pendant la chauffe) :
#   sma, ema, rsi, atr, bollinger, macd, highest, lowest, crossover, crossunder
from mt5.engine.indicators import atr, crossover, crossunder, ema, rsi  # noqa: F401
from mt5.engine.strategy import Context, ExitSignal, Signal, Strategy


class TemplateStrategy(Strategy):
    """Squelette commenté : montre chaque point d'accroche du moteur."""

    # Nom = nom du module (mt5/strategies/<name>.py). Sert dans --strategy.
    name = "template"

    # Paramètres et valeurs par défaut. Les valeurs passées via
    # ``--params cle=valeur,...`` sont converties vers le type du défaut
    # (int, float, bool, str). Ajoutez/retirez librement des clés.
    default_params: dict[str, Any] = {
        "ema_period": 50,      # exemple : filtre de tendance
        "rsi_period": 14,      # exemple : oscillateur
        "rsi_buy": 30.0,       # exemple : seuil de survente
        "rsi_sell": 70.0,      # exemple : seuil de surachat
        "atr_period": 14,      # pour le SL
        "atr_mult": 2.0,       # SL = atr_mult × ATR
        "rr": 1.5,             # TP = rr × distance de SL
        "use_trailing": False, # active manage()
    }

    # ------------------------------------------------------------------ #
    # 1) Chauffe : nombre minimal de bougies avant la première décision.
    #    Le moteur ne vous appelle pas tant qu'il y a moins de bougies.
    # ------------------------------------------------------------------ #
    @property
    def warmup_bars(self) -> int:
        return max(
            int(self.params["ema_period"]),
            int(self.params["rsi_period"]),
            int(self.params["atr_period"]),
        ) + 1

    # ------------------------------------------------------------------ #
    # Utilitaire : calcule les indicateurs UNE fois pour toute la série
    # (ils sont causaux, donc sans regard vers le futur) et les met en cache.
    # En backtest, le cache est partagé entre toutes les bougies : c'est
    # ce qui rend l'exécution rapide. Lisez toujours l'indice ctx.index.
    # ------------------------------------------------------------------ #
    def _indicators(self, ctx: Context) -> dict[str, list]:
        closes = ctx.series("close")   # série complète des clôtures
        highs = ctx.series("high")
        lows = ctx.series("low")
        p = self.params
        return {
            "ema": ctx.cached(("ema", p["ema_period"]), lambda: ema(closes, int(p["ema_period"]))),
            "rsi": ctx.cached(("rsi", p["rsi_period"]), lambda: rsi(closes, int(p["rsi_period"]))),
            "atr": ctx.cached(("atr", p["atr_period"]), lambda: atr(highs, lows, closes, int(p["atr_period"]))),
        }

    # ------------------------------------------------------------------ #
    # 2) Décision d'ENTRÉE, appelée à la clôture de la bougie ctx.index
    #    UNIQUEMENT s'il n'y a pas de position ouverte sur le symbole.
    #    Retournez Signal("buy"|"sell", sl=..., tp=..., reason="...") ou None.
    #    L'ordre est exécuté au marché à l'ouverture de la bougie suivante
    #    (backtest) ou immédiatement par le connecteur (live).
    # ------------------------------------------------------------------ #
    def on_bar(self, ctx: Context) -> Signal | None:
        ind = self._indicators(ctx)
        i = ctx.index
        price = ctx.close                     # clôture de la bougie courante
        ema_v, rsi_v, atr_v = ind["ema"][i], ind["rsi"][i], ind["atr"][i]

        # Toujours vérifier que les indicateurs sont chauds (non None).
        if ema_v is None or rsi_v is None or atr_v is None or atr_v <= 0:
            return None

        # --- Exemple de logique (DÉSACTIVÉ : le gabarit ne trade pas) ------ #
        # distance = float(self.params["atr_mult"]) * atr_v
        # rr = float(self.params["rr"])
        # if price > ema_v and rsi_v < float(self.params["rsi_buy"]):
        #     return Signal("buy", sl=price - distance, tp=price + rr * distance,
        #                   reason=f"tendance haussière + RSI {rsi_v:.1f} survendu")
        # if price < ema_v and rsi_v > float(self.params["rsi_sell"]):
        #     return Signal("sell", sl=price + distance, tp=price - rr * distance,
        #                   reason=f"tendance baissière + RSI {rsi_v:.1f} suracheté")
        #
        # Autres exemples d'accès :
        #   ctx.candle.high / .low / .open       -> bougie courante
        #   ctx.candles[i - 1].close             -> clôture précédente
        #   ctx.closes                           -> clôtures jusqu'à i inclus
        #   crossover(ind["ema"], other, i)      -> croisement à l'indice i
        #   ctx.time.hour                        -> heure UTC de la bougie
        #   ctx.spec.point / ctx.spec.digits     -> caractéristiques du symbole
        #   ctx.account.equity                   -> équité du compte
        return None

    # ------------------------------------------------------------------ #
    # 3) SORTIE discrétionnaire, appelée à chaque clôture quand une
    #    position est ouverte (ctx.position renseigné : direction,
    #    price_open, sl, tp, time_open, volume_lots, position_id).
    #    Retournez ExitSignal("raison") pour clôturer au marché, sinon None.
    #    Le SL/TP « durs » sont gérés par le broker : inutile de les recoder.
    # ------------------------------------------------------------------ #
    def should_exit(self, ctx: Context) -> ExitSignal | None:
        if ctx.position is None:
            return None
        # Exemple : sortir si le RSI revient au neutre.
        # rsi_v = self._indicators(ctx)["rsi"][ctx.index]
        # if rsi_v is not None and ctx.position.direction == "buy" and rsi_v > 50:
        #     return ExitSignal("RSI revenu au-dessus de 50")
        return None

    # ------------------------------------------------------------------ #
    # 4) GESTION de la position (trailing stop, prise partielle de profit
    #    non supportée). Retournez {"sl": nouveau_sl, "tp": nouveau_tp} ou
    #    None. Le moteur n'émet une modification que si un niveau change
    #    d'au moins 1 point. Ne déplacez le SL que dans le sens favorable.
    # ------------------------------------------------------------------ #
    def manage(self, ctx: Context) -> dict[str, float | None] | None:
        if not self.params.get("use_trailing") or ctx.position is None:
            return None
        atr_v = self._indicators(ctx)["atr"][ctx.index]
        if atr_v is None:
            return None
        pos = ctx.position
        trail = float(self.params["atr_mult"]) * atr_v
        if pos.direction == "buy":
            new_sl = ctx.close - trail
            if pos.sl is None or new_sl > pos.sl:
                return {"sl": new_sl, "tp": pos.tp}
        else:
            new_sl = ctx.close + trail
            if pos.sl is None or new_sl < pos.sl:
                return {"sl": new_sl, "tp": pos.tp}
        return None


STRATEGY = TemplateStrategy
