"""Tests de bout en bout du cycle de décision (run_cycle et CLI main)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mt5.engine.candles import Candle, candles_to_json
from mt5.engine.cycle import main, run_cycle
from mt5.engine.indicators import crossover, ema
from mt5.engine.risk import RiskConfig
from mt5.engine.strategy import Context, ExitSignal, Signal, Strategy, SymbolSpec

BASE = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)  # lundi
SPEC = SymbolSpec.default_fx("EURUSD")
LAST_OPEN = BASE + timedelta(hours=29)  # 2026-09-22 05:00 UTC (mardi)
NOW = LAST_OPEN + timedelta(hours=1, minutes=3)


def candles(n: int = 30, price: float = 1.10000) -> list[Candle]:
    return [Candle(BASE + timedelta(hours=i), price, price + 0.0005, price - 0.0005, price, 100, 0, 6) for i in range(n)]


def account(equity: float = 10_000.0) -> dict:
    return {
        "cashBalance": equity, "creditBalance": 0, "equity": equity, "marginFree": equity,
        "marginRequirement": 0, "leverage": "1000", "currencyCode": "USD",
        "asOf": NOW.isoformat(),
    }


def quote(**overrides) -> dict:
    q = {
        "symbol": "EURUSD", "bid": 1.10000, "ask": 1.10006, "last": 0, "spread": 6e-05, "point": 1e-05,
        "time": NOW.isoformat(), "tradeMode": "full", "tradeSessionOpen": True, "tradeSessionOpens": None,
    }
    q.update(overrides)
    return q


def position(time_create: datetime, direction: str = "buy", sl: float = 1.09500, tp: float = 1.11000) -> dict:
    return {
        "positionId": 123, "symbol": "EURUSD", "direction": direction, "volumeLots": 0.1,
        "priceOpen": 1.10000, "priceCurrent": 1.10000, "unrealisedPnl": 0.0, "swap": 0,
        "priceSL": sl, "priceTP": tp, "timeCreate": time_create.isoformat(),
    }


class AlwaysBuy(Strategy):
    name = "always_buy"

    def on_bar(self, ctx: Context):
        return Signal("buy", sl=ctx.close - 0.0050, tp=ctx.close + 0.0100, reason="test achat")


class AlwaysSell(Strategy):
    name = "always_sell"

    def on_bar(self, ctx: Context):
        return Signal("sell", sl=ctx.close + 0.0050, tp=ctx.close - 0.0100, reason="test vente")


class ExitNow(Strategy):
    name = "exit_now"

    def on_bar(self, ctx: Context):
        return Signal("buy", sl=ctx.close - 0.0050, tp=ctx.close + 0.0100, reason="test")

    def should_exit(self, ctx: Context):
        return ExitSignal("sortie test")


class Trailing(Strategy):
    name = "trailing"
    default_params = {"delta": 0.0010}

    def manage(self, ctx: Context):
        assert ctx.position is not None
        return {"sl": ctx.position.sl + float(self.params["delta"]), "tp": ctx.position.tp}


class RunCycleTests(unittest.TestCase):
    def _run(self, strategy, positions=None, quote_raw=None, risk=None, fills=None, acc=None, cnd=None):
        return run_cycle(
            strategy, "EURUSD", cnd or candles(), positions or [], acc or account(),
            quote() if quote_raw is None else quote_raw, SPEC, risk or RiskConfig(), fills_raw=fills, now=NOW,
        )

    def test_open_emitted(self):
        res = self._run(AlwaysBuy())
        self.assertEqual(res["schemaVersion"], 1)
        self.assertEqual(len(res["actions"]), 1)
        act = res["actions"][0]
        self.assertEqual(act["type"], "open")
        self.assertEqual(act["direction"], "buy")
        # 100 USD / (|1.10006 - 1.09500| = 0.00506 × 100 000 = 506) = 0.1976 → 0.19
        self.assertAlmostEqual(act["volumeLots"], 0.19)
        self.assertEqual(act["priceSL"], 1.095)
        self.assertEqual(act["priceTP"], 1.11)
        self.assertEqual(res["blocked"], [])
        self.assertEqual(res["asOf"], (LAST_OPEN + timedelta(hours=1)).isoformat())
        self.assertEqual(res["account"]["currency"], "USD")
        self.assertEqual(res["lastCandle"]["time"], LAST_OPEN.isoformat())

    def test_blocked_by_daily_loss(self):
        fills = [{"profit": -350.0, "time": NOW.isoformat()}]
        res = self._run(AlwaysBuy(), fills=fills, risk=RiskConfig(max_daily_loss_pct=3.0))
        self.assertEqual(res["actions"], [])
        self.assertEqual([b["rule"] for b in res["blocked"]], ["max_daily_loss"])

    def test_no_reentry_when_position_created_after_bar_open(self):
        # max_positions=2 pour isoler la règle de ré-entrée du plafond de positions
        pos = [position(LAST_OPEN + timedelta(minutes=3))]
        res = self._run(AlwaysBuy(), positions=pos, risk=RiskConfig(max_positions_per_symbol=2))
        self.assertFalse([a for a in res["actions"] if a["type"] == "open"])
        self.assertTrue(any("consommé" in w for w in res["warnings"]))
        # position créée AVANT la dernière bougie → ré-entrée autorisée (2e position)
        pos = [position(LAST_OPEN - timedelta(hours=5))]
        res = self._run(AlwaysBuy(), positions=pos, risk=RiskConfig(max_positions_per_symbol=2))
        self.assertEqual([a["type"] for a in res["actions"]], ["open"])

    def test_max_positions_default_blocks_second_open(self):
        res = self._run(AlwaysBuy(), positions=[position(LAST_OPEN - timedelta(hours=5))])
        self.assertEqual(res["actions"], [])
        self.assertEqual(res["positions"], 1)

    def test_close_emitted_on_exit_signal(self):
        res = self._run(ExitNow(), positions=[position(LAST_OPEN - timedelta(hours=5))])
        self.assertEqual(len(res["actions"]), 1)
        self.assertEqual(res["actions"][0]["type"], "close")
        self.assertEqual(res["actions"][0]["positionId"], 123)
        self.assertEqual(res["actions"][0]["reason"], "sortie test")

    def test_modify_emitted_on_trailing(self):
        res = self._run(Trailing(), positions=[position(LAST_OPEN - timedelta(hours=5))])
        self.assertEqual(len(res["actions"]), 1)
        act = res["actions"][0]
        self.assertEqual(act["type"], "modify")
        self.assertEqual(act["positionId"], 123)
        self.assertAlmostEqual(act["priceSL"], 1.09600)
        self.assertAlmostEqual(act["priceTP"], 1.11000)
        # variation < 1 point → pas de modification
        res = self._run(Trailing({"delta": 0.000004}), positions=[position(LAST_OPEN - timedelta(hours=5))])
        self.assertEqual(res["actions"], [])

    def test_longonly_blocks_sell(self):
        res = self._run(AlwaysSell(), quote_raw=quote(tradeMode="longonly"))
        self.assertEqual(res["actions"], [])
        self.assertEqual([b["rule"] for b in res["blocked"]], ["trade_mode"])
        res = self._run(AlwaysBuy(), quote_raw=quote(tradeMode="longonly"))
        self.assertEqual(res["actions"][0]["type"], "open")

    def test_closed_session_blocks_open_but_allows_close(self):
        q = quote(tradeSessionOpen=False, tradeSessionOpens="2026-09-27T21:01:00+00:00")
        res = self._run(ExitNow(), positions=[position(LAST_OPEN - timedelta(hours=5))], quote_raw=q,
                        risk=RiskConfig(max_positions_per_symbol=2))
        types = [a["type"] for a in res["actions"]]
        self.assertEqual(types, ["close"])
        self.assertIn("session_closed", [b["rule"] for b in res["blocked"]])

    def test_spread_hours_friday_guards(self):
        res = self._run(AlwaysBuy(), quote_raw=quote(spread=0.00020), risk=RiskConfig(max_spread_points=15))
        self.assertEqual([b["rule"] for b in res["blocked"]], ["max_spread"])
        res = self._run(AlwaysBuy(), risk=RiskConfig(trading_hours_utc=(7, 21)))  # NOW = 06:03
        self.assertEqual([b["rule"] for b in res["blocked"]], ["trading_hours"])
        friday = NOW.replace(day=25, hour=20)
        res = run_cycle(AlwaysBuy(), "EURUSD", candles(), [], account(), quote(), SPEC, RiskConfig(), now=friday)
        self.assertEqual([b["rule"] for b in res["blocked"]], ["friday_cutoff"])

    def test_margin_reduces_lots(self):
        acc = account(10_000)
        acc["marginFree"] = 10.0  # 0.8 × 10 = 8 USD de marge → 8 / (100 000 × 1.10006 / 1000) = 0.0727 → 0.07
        res = self._run(AlwaysBuy(), acc=acc)
        self.assertEqual(res["actions"][0]["volumeLots"], 0.07)
        self.assertTrue(any("marge" in w for w in res["warnings"]))
        acc["marginFree"] = 0.5
        res = self._run(AlwaysBuy(), acc=acc)
        self.assertEqual([b["rule"] for b in res["blocked"]], ["margin"])

    def test_not_enough_bars(self):
        class Slow(AlwaysBuy):
            @property
            def warmup_bars(self):
                return 100

        res = self._run(Slow())
        self.assertEqual(res["actions"], [])
        self.assertTrue(any("chauffe" in w for w in res["warnings"]))

    def test_empty_candles_raises(self):
        with self.assertRaises(ValueError):
            run_cycle(AlwaysBuy(), "EURUSD", [], [], account(), quote(), SPEC, RiskConfig())

    def test_positions_other_symbol_ignored_and_bad_entries_warn(self):
        pos = [position(LAST_OPEN - timedelta(hours=5)), {"bizarre": True}]
        pos[0]["symbol"] = "GBPUSD"
        res = self._run(AlwaysBuy(), positions=pos)
        self.assertEqual(res["positions"], 0)
        self.assertEqual(res["actions"][0]["type"], "open")
        self.assertTrue(any("illisible" in w for w in res["warnings"]))

    def test_never_more_than_one_open(self):
        res = self._run(AlwaysBuy(), risk=RiskConfig(max_positions_per_symbol=5))
        self.assertEqual(sum(1 for a in res["actions"] if a["type"] == "open"), 1)

    def test_stops_validated_against_bid_ask_not_entry(self):
        class SlBetweenBidAsk(Strategy):
            name = "sl_between"

            def on_bar(self, ctx: Context):
                return Signal("buy", sl=ctx.close + 0.00003, tp=ctx.close + 0.0100)  # bid 1.10000 < SL 1.10003 < ask 1.10006

        res = self._run(SlBetweenBidAsk())
        self.assertEqual(res["actions"], [])
        self.assertEqual([b["rule"] for b in res["blocked"]], ["invalid_stops"])

        class TpTooClose(Strategy):
            name = "tp_close"

            def on_bar(self, ctx: Context):
                return Signal("buy", sl=ctx.close - 0.0050, tp=ctx.close + 0.00008)

        spec = SymbolSpec.default_fx("EURUSD")
        spec.stops_level = 10
        res = run_cycle(TpTooClose(), "EURUSD", candles(), [], account(), quote(), spec, RiskConfig(), now=NOW)
        self.assertEqual(res["actions"], [])
        self.assertEqual([b["rule"] for b in res["blocked"]], ["invalid_stops"])
        # Vente : SL sous le ask
        class SellSlBelowAsk(Strategy):
            name = "sell_bad"

            def on_bar(self, ctx: Context):
                return Signal("sell", sl=ctx.close + 0.00004, tp=ctx.close - 0.0100)

        res = self._run(SellSlBelowAsk())
        self.assertEqual([b["rule"] for b in res["blocked"]], ["invalid_stops"])

    def test_missing_fills_warns_daily_loss_not_checked(self):
        res = self._run(AlwaysBuy(), fills=None, risk=RiskConfig(max_daily_loss_pct=3.0))
        self.assertEqual(res["actions"][0]["type"], "open")
        self.assertTrue(any("fills.json absent" in w for w in res["warnings"]))
        res = self._run(AlwaysBuy(), fills=[], risk=RiskConfig(max_daily_loss_pct=3.0))
        self.assertFalse(any("fills.json absent" in w for w in res["warnings"]))
        res = self._run(AlwaysBuy(), fills=None, risk=RiskConfig(max_daily_loss_pct=0))
        self.assertFalse(any("fills.json absent" in w for w in res["warnings"]))

    def test_modify_blocked_when_new_sl_above_bid(self):
        pos = [position(LAST_OPEN - timedelta(hours=5), sl=1.09500)]
        res = self._run(Trailing({"delta": 0.0060}), positions=pos)  # SL 1.10100 > bid 1.10000 pour un achat
        self.assertEqual(res["actions"], [])
        self.assertIn("invalid_stops", [b["rule"] for b in res["blocked"]])
        self.assertTrue(any("modification de 123" in w for w in res["warnings"]))

    def test_max_positions_reported_in_blocked(self):
        res = self._run(AlwaysBuy(), positions=[position(LAST_OPEN - timedelta(hours=5))])
        self.assertEqual(res["actions"], [])
        self.assertEqual([b["rule"] for b in res["blocked"]], ["max_positions"])

    def test_daily_loss_reference_is_day_start_balance(self):
        # Perte de 295 déjà déduite du solde (9 705) : limite = 3 % de 10 000 → pas bloqué (comme le backtest).
        fills = [{"profit": -295.0, "time": NOW.isoformat()}]
        res = self._run(AlwaysBuy(), acc=account(10_000 - 295), fills=fills, risk=RiskConfig(max_daily_loss_pct=3.0))
        self.assertEqual(res["blocked"], [])
        fills = [{"profit": -300.0, "time": NOW.isoformat()}]
        res = self._run(AlwaysBuy(), acc=account(10_000 - 300), fills=fills, risk=RiskConfig(max_daily_loss_pct=3.0))
        self.assertEqual([b["rule"] for b in res["blocked"]], ["max_daily_loss"])

    def test_modify_serializes_unset_levels_as_zero(self):
        pos = position(LAST_OPEN - timedelta(hours=5), sl=1.09500)
        pos["priceTP"] = None
        res = self._run(Trailing(), positions=[pos])
        act = res["actions"][0]
        self.assertEqual(act["type"], "modify")
        self.assertEqual(act["priceTP"], 0.0)
        self.assertIsInstance(act["priceTP"], float)
        pos["priceTP"] = 0
        res = self._run(Trailing(), positions=[pos])
        self.assertEqual(res["actions"][0]["priceTP"], 0.0)

    def test_open_serializes_missing_tp_as_zero(self):
        class NoTp(Strategy):
            name = "no_tp"

            def on_bar(self, ctx: Context):
                return Signal("buy", sl=ctx.close - 0.0050, tp=None)

        res = self._run(NoTp())
        self.assertEqual(res["actions"][0]["priceTP"], 0.0)

    def test_now_prefers_account_asof_over_stale_quote_time(self):
        acc = account()
        acc["asOf"] = "2026-09-22T07:03:10+00:00"
        q = quote(time="2026-09-22T06:59:58+00:00")  # dernier tick de l'heure précédente
        res = run_cycle(AlwaysBuy(), "EURUSD", candles(), [], acc, q, SPEC, RiskConfig(trading_hours_utc=(7, 21)))
        self.assertEqual(res["blocked"], [])
        self.assertEqual(res["actions"][0]["type"], "open")
        # Passage de minuit : la perte de la veille ne compte plus au cycle de 00:03.
        acc["asOf"] = "2026-09-23T00:03:10+00:00"
        q = quote(time="2026-09-22T23:59:57+00:00")
        fills = [{"profit": -400.0, "time": "2026-09-22T15:00:00+00:00"}]
        res = run_cycle(AlwaysBuy(), "EURUSD", candles(), [], acc, q, SPEC, RiskConfig(max_daily_loss_pct=3.0), fills_raw=fills)
        self.assertEqual(res["blocked"], [])
        # Sans asOf, repli sur l'heure de la cotation.
        del acc["asOf"]
        res = run_cycle(AlwaysBuy(), "EURUSD", candles(), [], acc, q, SPEC, RiskConfig(max_daily_loss_pct=3.0), fills_raw=fills)
        self.assertEqual([b["rule"] for b in res["blocked"]], ["max_daily_loss"])

    def test_modify_warns_when_session_closed(self):
        res = self._run(Trailing(), positions=[position(LAST_OPEN - timedelta(hours=5))],
                        quote_raw=quote(tradeSessionOpen=False))
        self.assertEqual([a["type"] for a in res["actions"]], ["modify"])
        self.assertTrue(any("session fermée" in w and "modification" in w for w in res["warnings"]))


def ema_cross_candles() -> list[Candle]:
    """Série synthétique dont la DERNIÈRE bougie porte un croisement EMA9 > EMA21."""
    prices = [1.10000 - 0.0010 * i for i in range(30)] + [1.07100 + 0.0030 * i for i in range(30)]
    fast, slow = ema(prices, 9), ema(prices, 21)
    idx = next(i for i in range(len(prices)) if crossover(fast, slow, i))
    out = []
    for i, p in enumerate(prices[: idx + 1]):
        out.append(Candle(BASE + timedelta(hours=i), p, p + 0.0008, p - 0.0008, p, 100, 0, 6))
    return out


class CycleCliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._err = io.StringIO()
        self._ctx = contextlib.redirect_stderr(self._err)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def _write(self, name: str, payload) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _argv(self, cnd, positions, extra=()):
        last_open = cnd[-1].time
        now = last_open + timedelta(hours=1, minutes=3)
        bid = round(cnd[-1].close, 5)
        q = quote(time=now.isoformat(), bid=bid, ask=round(bid + 0.00006, 5))
        acc = account()
        acc["asOf"] = now.isoformat()
        spec = {
            "symbol": "EURUSD", "digits": 5, "contractSize": 100000, "currencyBase": "EUR", "currencyProfit": "USD",
            "volumeMinLots": 0.01, "volumeMaxLots": 100, "volumeStepLots": 0.01, "stopsLevel": 1, "freezeLevel": 0,
            "tradeMode": "full",
        }
        self.out = os.path.join(self.dir, "actions.json")
        return [
            "--strategy", "ema_cross", "--symbol", "EURUSD",
            "--candles", self._write("candles.json", candles_to_json(cnd)),
            "--positions", self._write("positions.json", positions),
            "--account", self._write("account.json", acc),
            "--quote", self._write("quote.json", q),
            "--spec", self._write("symbol.json", spec),
            "--out", self.out, "--quiet", *extra,
        ]

    def _read(self):
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh)

    def test_cli_open_with_ema_cross(self):
        cnd = ema_cross_candles()
        code = main(self._argv(cnd, [], ["--max-spread-points", "15", "--hours", "0-24"]))
        self.assertEqual(code, 0)
        res = self._read()
        self.assertEqual(res["strategy"], "ema_cross")
        self.assertEqual(len(res["actions"]), 1)
        act = res["actions"][0]
        self.assertEqual((act["type"], act["direction"]), ("open", "buy"))
        self.assertGreater(act["volumeLots"], 0)
        ask = round(cnd[-1].close + 0.00006, 5)
        self.assertLess(act["priceSL"], ask)
        self.assertGreater(act["priceTP"], ask)
        self.assertLessEqual(len(str(act["priceSL"]).split(".")[1]), 5)  # arrondi aux décimales du symbole

    def test_cli_no_reentry_with_position(self):
        cnd = ema_cross_candles()
        pos = [position(cnd[-1].time + timedelta(minutes=2))]
        self.assertEqual(main(self._argv(cnd, pos)), 0)
        res = self._read()
        self.assertFalse([a for a in res["actions"] if a["type"] == "open"])

    def test_cli_blocked_by_daily_loss_still_exit_0(self):
        cnd = ema_cross_candles()
        now = cnd[-1].time + timedelta(hours=1)
        fills = self._write("fills.json", [{"profit": -400.0, "time": now.isoformat()}, {"inconnu": 1}])
        code = main(self._argv(cnd, [], ["--fills", fills, "--max-daily-loss-pct", "3"]))
        self.assertEqual(code, 0)
        res = self._read()
        self.assertEqual(res["actions"], [])
        self.assertEqual(res["blocked"][0]["rule"], "max_daily_loss")
        self.assertTrue(any("illisible" in w for w in res["warnings"]))

    def test_cli_invalid_input(self):
        self.assertEqual(main(["--strategy", "ema_cross", "--symbol", "EURUSD", "--candles", "/nexiste/pas.json",
                               "--positions", "/nexiste/pas.json", "--account", "/nexiste/pas.json"]), 2)
        empty = self._write("empty.json", [])
        acc = self._write("account.json", account())
        self.assertEqual(main(["--strategy", "ema_cross", "--symbol", "EURUSD", "--candles", empty,
                               "--positions", empty, "--account", acc]), 2)
        self.assertIn("Erreur d'entrée", self._err.getvalue())

    def test_cli_help_in_french(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertIn("utilisation :", text)
        self.assertIn("afficher cette aide", text)
        for english in ("usage:", "options:", "show this help"):
            self.assertNotIn(english, text)

    def test_cli_with_patched_strategy_registry(self):
        cnd = candles()
        with mock.patch("mt5.strategies.registry.get_strategy", return_value=ExitNow()):
            code = main(self._argv(cnd, [position(cnd[-1].time - timedelta(hours=3))]))
        self.assertEqual(code, 0)
        res = self._read()
        self.assertEqual([a["type"] for a in res["actions"]], ["close"])


if __name__ == "__main__":
    unittest.main()
