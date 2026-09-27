"""Tests du backtester sur des bougies synthétiques déterministes."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from mt5.engine.backtest import Backtester, main
from mt5.engine.candles import Candle
from mt5.engine.risk import RiskConfig
from mt5.engine.strategy import Context, ExitSignal, Signal, Strategy, SymbolSpec

BASE = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)  # lundi
SPEC = SymbolSpec.default_fx("EURUSD")


def mk(i: int, o: float, h: float, l: float, c: float, spread: int = 10) -> Candle:
    return Candle(BASE + timedelta(hours=i), o, h, l, c, 100, 0, spread)


def flat(i: int, price: float = 1.10000, spread: int = 10) -> Candle:
    return mk(i, price, price, price, price, spread)


class ScriptedStrategy(Strategy):
    """Émet des signaux à des indices précis (pour des scénarios déterministes)."""

    name = "scripted"

    def __init__(self, entries=None, exits=None, manages=None):
        super().__init__({})
        self.entries = entries or {}
        self.exits = set(exits or ())
        self.manages = manages or {}

    def on_bar(self, ctx: Context):
        return self.entries.get(ctx.index)

    def should_exit(self, ctx: Context):
        return ExitSignal("sortie scriptée") if ctx.index in self.exits else None

    def manage(self, ctx: Context):
        return self.manages.get(ctx.index)


class BacktestScenarioTests(unittest.TestCase):
    def test_buy_hits_tp_with_spread_cost(self):
        # Signal à la clôture de la bougie 1 (1.10000) ; entrée à l'ouverture de la 2 au ask.
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10200, 1.09900, 1.10100),       # entrée 1.10000 + 10 pts = 1.10010
            mk(3, 1.10100, 1.11050, 1.10050, 1.11000),       # high >= TP 1.11000 → TP
            flat(4, 1.11000),
        ]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=1.09500, tp=1.11000, reason="test")})
        res = Backtester(strat, candles, SPEC, RiskConfig(risk_pct_per_trade=1.0), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        t = res.trades[0]
        self.assertAlmostEqual(t.entry_price, 1.10010)  # coût du spread appliqué
        self.assertEqual(t.entry_time, candles[2].time)
        self.assertEqual(t.exit_time, candles[3].time)
        self.assertAlmostEqual(t.exit_price, 1.11000)
        self.assertEqual(t.reason, "take profit")
        # 100 USD de risque / (0.0051 × 100 000 = 510) = 0.196 → 0.19 lot
        self.assertAlmostEqual(t.lots, 0.19)
        self.assertAlmostEqual(t.pnl, (1.11000 - 1.10010) * 0.19 * 100_000, places=6)
        self.assertAlmostEqual(res.net_pnl, t.pnl, places=6)
        self.assertAlmostEqual(res.final_equity, 10_000 + t.pnl, places=6)
        self.assertEqual(res.wins, 1)
        self.assertEqual(res.win_rate, 1.0)
        self.assertIsNone(res.profit_factor)  # aucune perte
        self.assertEqual(res.max_drawdown_abs, 0.0)

    def test_sl_and_tp_same_bar_takes_sl(self):
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10100, 1.09900, 1.10000),
            mk(3, 1.10000, 1.11100, 1.09400, 1.10000),  # SL 1.09500 ET TP 1.11000 touchés → SL
            flat(4),
        ]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=1.09500, tp=1.11000)})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        t = res.trades[0]
        self.assertEqual(t.reason, "stop loss")
        self.assertAlmostEqual(t.exit_price, 1.09500)
        self.assertLess(t.pnl, 0)
        self.assertEqual(res.losses, 1)
        # perte ≈ 1 % (0.19 lot × 0.0051 = 96.9 USD à cause de l'arrondi vers le bas)
        self.assertAlmostEqual(t.pnl, -(1.10010 - 1.09500) * 0.19 * 100_000, places=6)

    def test_sell_fill_and_asymmetric_stops(self):
        # Vente : entrée au bid (open) ; SL touché si high + spread >= sl.
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10000, 1.09800, 1.09900),
            mk(3, 1.09900, 1.10495, 1.09800, 1.10400),  # high + 10 pts = 1.10505 >= SL 1.10500 → SL
            flat(4),
        ]
        strat = ScriptedStrategy(entries={1: Signal("sell", sl=1.10500, tp=1.09000)})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        t = res.trades[0]
        self.assertAlmostEqual(t.entry_price, 1.10000)  # pas de spread à l'entrée d'une vente
        self.assertEqual(t.reason, "stop loss")
        self.assertLess(t.pnl, 0)

    def test_sell_tp_needs_spread(self):
        # TP vente à 1.09500 : low + spread doit être <= tp ; low 1.09495 + 0.00010 = 1.09505 → pas touché
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10000, 1.09495, 1.09600),
            mk(3, 1.09600, 1.09700, 1.09480, 1.09600),  # 1.09480 + 0.00010 = 1.09490 <= TP → touché
            flat(4, 1.09600),
        ]
        strat = ScriptedStrategy(entries={1: Signal("sell", sl=1.10500, tp=1.09500)})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.trades[0].exit_time, candles[3].time)
        self.assertEqual(res.trades[0].reason, "take profit")

    def test_discretionary_exit_at_next_open(self):
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10100, 1.09950, 1.10050),
            mk(3, 1.10050, 1.10150, 1.10000, 1.10100),  # should_exit ici (index 3)
            mk(4, 1.10120, 1.10200, 1.10100, 1.10150),  # sortie à l'ouverture : 1.10120 (bid)
            flat(5, 1.10150),
        ]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=1.09500, tp=1.12000)}, exits={3})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        t = res.trades[0]
        self.assertEqual(t.exit_time, candles[4].time)
        self.assertAlmostEqual(t.exit_price, 1.10120)
        self.assertEqual(t.reason, "sortie scriptée")

    def test_manage_trailing_moves_sl(self):
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10300, 1.09950, 1.10250),
            mk(3, 1.10250, 1.10400, 1.10200, 1.10350),  # manage : SL remonté à 1.10200
            mk(4, 1.10350, 1.10360, 1.10150, 1.10200),  # low 1.10150 <= 1.10200 → SL (nouveau) touché
            flat(5, 1.10200),
        ]
        strat = ScriptedStrategy(
            entries={1: Signal("buy", sl=1.09500, tp=1.12000)},
            manages={3: {"sl": 1.10200, "tp": 1.12000}},
        )
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        self.assertEqual(res.trades[0].reason, "stop loss")
        self.assertAlmostEqual(res.trades[0].exit_price, 1.10200)
        self.assertGreater(res.trades[0].pnl, 0)

    def test_drawdown_and_stats(self):
        # Trade 1 : perte (SL) ; trade 2 : gain (TP). Drawdown = perte du trade 1.
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10050, 1.09950, 1.10000),
            mk(3, 1.10000, 1.10050, 1.09400, 1.09500),   # SL 1.09500
            flat(4, 1.09500), flat(5, 1.09500),
            mk(6, 1.09500, 1.09600, 1.09450, 1.09550),   # entrée 2 à 1.09510
            mk(7, 1.09550, 1.10600, 1.09500, 1.10500),   # TP 1.10500
            flat(8, 1.10500),
        ]
        strat = ScriptedStrategy(entries={
            1: Signal("buy", sl=1.09500, tp=1.11000),
            5: Signal("buy", sl=1.09000, tp=1.10500),
        })
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 2)
        self.assertEqual((res.wins, res.losses), (1, 1))
        loss = res.trades[0].pnl
        win = res.trades[1].pnl
        self.assertLess(loss, 0)
        self.assertGreater(win, 0)
        self.assertAlmostEqual(res.max_drawdown_abs, -loss, places=6)
        self.assertAlmostEqual(res.max_drawdown_pct, -loss / 10_000, places=9)
        self.assertAlmostEqual(res.gross_profit, win)
        self.assertAlmostEqual(res.gross_loss, loss)
        self.assertAlmostEqual(res.profit_factor, win / -loss)
        self.assertAlmostEqual(res.expectancy, (win + loss) / 2)
        self.assertAlmostEqual(res.avg_win, win)
        self.assertAlmostEqual(res.avg_loss, loss)
        self.assertEqual(len(res.equity_curve), 3)  # départ + 2 trades
        self.assertEqual(res.equity_curve[-1]["equity"], round(res.final_equity, 2))
        # sérialisable en JSON
        json.dumps(res.to_dict())
        self.assertIn("RÉSULTATS", res.format_summary())

    def test_end_of_data_closes_position(self):
        candles = [flat(0), flat(1), mk(2, 1.10000, 1.10100, 1.09950, 1.10080), mk(3, 1.10080, 1.10120, 1.10000, 1.10100)]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=1.09500, tp=1.12000)})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        self.assertEqual(res.trades[0].reason, "fin des données")
        self.assertAlmostEqual(res.trades[0].exit_price, 1.10100)

    def test_signal_without_sl_is_skipped(self):
        candles = [flat(0), flat(1), flat(2), flat(3)]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=None, tp=1.12)})
        res = Backtester(strat, candles, SPEC, RiskConfig(), 10_000).run()
        self.assertEqual(res.n_trades, 0)
        self.assertTrue(res.warnings)

    def test_guards_block_entries(self):
        candles = [flat(i, spread=30) for i in range(5)]
        strat = ScriptedStrategy(entries={1: Signal("buy", sl=1.09500, tp=1.11000)})
        res = Backtester(strat, candles, SPEC, RiskConfig(max_spread_points=15), 10_000).run()
        self.assertEqual(res.n_trades, 0)
        self.assertEqual(res.blocked.get("max_spread"), 1)
        # heures : la décision de la bougie 1 tombe à 02:00 UTC, hors 7h-21h
        res = Backtester(strat, candles, SPEC, RiskConfig(trading_hours_utc=(7, 21)), 10_000).run()
        self.assertEqual(res.blocked.get("trading_hours"), 1)

    def test_daily_loss_guard_in_backtest(self):
        # Perte de 1 % puis nouveau signal le même jour avec limite 0.5 % → bloqué.
        candles = [
            flat(0), flat(1),
            mk(2, 1.10000, 1.10050, 1.09950, 1.10000),
            mk(3, 1.10000, 1.10050, 1.09400, 1.09500),
            flat(4, 1.09500), flat(5, 1.09500), flat(6, 1.09500),
        ]
        strat = ScriptedStrategy(entries={
            1: Signal("buy", sl=1.09500, tp=1.11000),
            5: Signal("buy", sl=1.09000, tp=1.10500),
        })
        res = Backtester(strat, candles, SPEC, RiskConfig(max_daily_loss_pct=0.5), 10_000).run()
        self.assertEqual(res.n_trades, 1)
        self.assertEqual(res.blocked.get("max_daily_loss"), 1)


class BacktestCliTests(unittest.TestCase):
    """Les appels CLI sont exécutés avec la sortie standard/erreur capturée."""

    def setUp(self):
        self._out, self._err = io.StringIO(), io.StringIO()
        self._ctx = contextlib.ExitStack()
        self._ctx.enter_context(contextlib.redirect_stdout(self._out))
        self._ctx.enter_context(contextlib.redirect_stderr(self._err))
        self.addCleanup(self._ctx.close)

    def test_cli_on_sample(self):
        sample = os.path.join(os.path.dirname(__file__), "..", "data", "EURUSD_H1_sample.json")
        if not os.path.exists(sample):
            self.skipTest("fichier d'exemple absent")
        out = os.path.join(tempfile.mkdtemp(), "bt.json")
        code = main(["--strategy", "ema_cross", "--candles", sample, "--symbol", "EURUSD", "--balance", "10000", "--params", "fast=5,slow=10", "--json", out])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["nBars"], 45)
        self.assertEqual(data["params"]["fast"], 5)
        self.assertIn("RÉSULTATS DU BACKTEST", self._out.getvalue())

    def test_cli_invalid_strategy(self):
        sample = os.path.join(os.path.dirname(__file__), "..", "data", "EURUSD_H1_sample.json")
        if not os.path.exists(sample):
            self.skipTest("fichier d'exemple absent")
        self.assertEqual(main(["--strategy", "inexistante", "--candles", sample]), 2)

    def test_cli_missing_file(self):
        self.assertEqual(main(["--strategy", "ema_cross", "--candles", "/chemin/inexistant.json"]), 2)


if __name__ == "__main__":
    unittest.main()
