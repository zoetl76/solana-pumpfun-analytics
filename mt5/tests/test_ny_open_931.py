"""Tests de la stratégie « NY Open 9:31 » : horaires, plan, supervision, backtest, CLI."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from mt5.engine import backtest_nyopen as bt
from mt5.engine import nyopen_cycle as cli
from mt5.engine.candles import Candle, candle_from_dict
from mt5.engine.strategy import PositionState, SymbolSpec
from mt5.strategies import ny_open_931 as ny

SPEC = SymbolSpec(
    symbol="XAUUSD", digits=2, point=0.01, contract_size=100.0, currency_base="XAU",
    currency_profit="USD", volume_min=0.01, volume_max=20.0, volume_step=0.01, stops_level=1,
)
CFG = ny.NyOpenConfig()
UTC = timezone.utc


def bar(t: datetime, o: float, h: float, l: float, c: float, spread: int = 16) -> Candle:
    return Candle(time=t, open=o, high=h, low=l, close=c, tick_volume=100, volume=0.0, spread_points=spread)


def ref_at(day: date) -> datetime:
    return ny.ref_bar_start(day, CFG)


def flat_bars(start: datetime, n: int, lo: float, hi: float, spread: int = 16) -> list[Candle]:
    mid = (lo + hi) / 2
    return [bar(start + timedelta(minutes=i), mid, hi, lo, mid, spread) for i in range(n)]


class HorairesTests(unittest.TestCase):
    def test_ref_bar_utc_follows_dst(self) -> None:
        self.assertEqual(ref_at(date(2026, 9, 28)), datetime(2026, 9, 28, 13, 31, tzinfo=UTC))
        self.assertEqual(ref_at(date(2026, 12, 15)), datetime(2026, 12, 15, 14, 31, tzinfo=UTC))

    def test_expiration_and_deadline(self) -> None:
        d = date(2026, 9, 28)
        self.assertEqual(ny.orders_expiration(d, CFG), datetime(2026, 9, 28, 14, 2, tzinfo=UTC))
        self.assertEqual(ny.hard_deadline(d, CFG), datetime(2026, 9, 28, 15, 2, tzinfo=UTC))

    def test_local_day(self) -> None:
        self.assertEqual(ny.local_day(datetime(2026, 9, 28, 2, 0, tzinfo=UTC), CFG), date(2026, 9, 27))

    def test_config_from_params_types_and_validation(self) -> None:
        cfg = ny.NyOpenConfig.from_params({"buffer_buy": "0.2", "rr": "3", "add_spread_to_buy": "false", "skip_dates": "2026-11-26"})
        self.assertEqual(cfg.buffer_buy, 0.2)
        self.assertEqual(cfg.rr, 3.0)
        self.assertFalse(cfg.add_spread_to_buy)
        self.assertEqual(cfg.skip_date_set(), {date(2026, 11, 26)})
        with self.assertRaises(ValueError):
            ny.NyOpenConfig.from_params({"inconnu": 1})
        with self.assertRaises(ValueError):
            ny.NyOpenConfig.from_params({"rr": 0})


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.day = date(2026, 9, 28)
        self.ref = bar(ref_at(self.day), 4298.0, 4300.0, 4297.0, 4299.0)

    def test_levels_sl_tp_lots(self) -> None:
        p = ny.plan(self.ref, 0.16, 10_000, SPEC, CFG)
        self.assertTrue(p.active)
        buy, sell = p.orders
        self.assertEqual(buy.type, "buy_stop")
        self.assertAlmostEqual(buy.price, 4300.26)
        self.assertAlmostEqual(buy.sl, 4297.0)
        self.assertAlmostEqual(buy.tp, 4306.78)  # 4300.26 + 2 × 3.26
        self.assertAlmostEqual(buy.lots, 0.15)  # 50 / (3.26 × 100) = 0.153 → 0.15
        self.assertEqual(sell.type, "sell_stop")
        self.assertAlmostEqual(sell.price, 4296.90)
        self.assertAlmostEqual(sell.sl, 4300.0)
        self.assertAlmostEqual(sell.tp, 4290.70)  # 4296.90 − 2 × 3.10
        self.assertAlmostEqual(sell.lots, 0.16)  # 50 / 310 = 0.161 → 0.16
        self.assertEqual(p.expiration, datetime(2026, 9, 28, 14, 2, tzinfo=UTC))
        args = buy.to_connector_args("XAUUSD")
        self.assertEqual(args["timeType"], "specified")
        self.assertEqual(args["expiration"], "2026-09-28T14:02:00Z")
        self.assertEqual(set(args), {"symbol", "type", "volumeLots", "priceOrder", "priceSL", "priceTP", "timeType", "expiration"})

    def test_buy_without_spread_option(self) -> None:
        cfg = ny.NyOpenConfig(add_spread_to_buy=False)
        p = ny.plan(self.ref, 0.16, 10_000, SPEC, cfg)
        self.assertAlmostEqual(p.orders[0].price, 4300.10)

    def test_skip_when_late(self) -> None:
        p = ny.plan(self.ref, 0.16, 10_000, SPEC, CFG, bid=4300.40, ask=4300.56)
        self.assertIsNotNone(p.skipped)
        self.assertIn("retard", p.skipped or "")
        p2 = ny.plan(self.ref, 0.16, 10_000, SPEC, CFG, bid=4296.50, ask=4296.66)
        self.assertIn("retard", p2.skipped or "")
        p3 = ny.plan(self.ref, 0.16, 10_000, SPEC, CFG, bid=4298.90, ask=4299.06)
        self.assertIsNone(p3.skipped)

    def test_skip_after_window(self) -> None:
        p = ny.plan(self.ref, 0.16, 10_000, SPEC, CFG, now=datetime(2026, 9, 28, 14, 3, tzinfo=UTC))
        self.assertIn("trop tard", p.skipped or "")

    def test_skip_wrong_bar_zero_range_and_dates(self) -> None:
        wrong = bar(ref_at(self.day) + timedelta(minutes=1), 4298, 4300, 4297, 4299)
        self.assertIn("incorrecte", ny.plan(wrong, 0.16, 10_000, SPEC, CFG).skipped or "")
        flat = bar(ref_at(self.day), 4298, 4298, 4298, 4298)
        self.assertIn("amplitude", ny.plan(flat, 0.16, 10_000, SPEC, CFG).skipped or "")
        cfg = ny.NyOpenConfig(skip_dates="2026-09-28")
        self.assertIn("exclue", ny.plan(self.ref, 0.16, 10_000, SPEC, cfg).skipped or "")
        cfg2 = ny.NyOpenConfig(max_range=2.0)
        self.assertIn("maximum", ny.plan(self.ref, 0.16, 10_000, SPEC, cfg2).skipped or "")

    def test_min_lot_warning_on_tiny_account(self) -> None:
        p = ny.plan(self.ref, 0.16, 300, SPEC, CFG)  # 1,5 $ de risque → 0.004 lot → 0.01 forcé
        self.assertTrue(p.active)
        self.assertEqual(p.orders[0].lots, 0.01)
        self.assertTrue(any("minimum" in w for w in p.orders[0].warnings))


def pos(pid: int, direction: str, t: datetime, sl: float = 4297.0, tp: float = 4306.78) -> PositionState:
    return PositionState(direction=direction, volume_lots=0.15, price_open=4300.26, sl=sl, tp=tp,
                         time_open=t, position_id=pid, symbol="XAUUSD")


def order(oid: int, otype: str, t: datetime) -> ny.PendingOrderState:
    return ny.PendingOrderState(order_id=oid, symbol="XAUUSD", type=otype, price=4296.9, time_setup=t)


class SuperviseTests(unittest.TestCase):
    day = date(2026, 9, 28)
    t_place = datetime(2026, 9, 28, 13, 32, 5, tzinfo=UTC)

    def test_waiting_before_expiration(self) -> None:
        now = datetime(2026, 9, 28, 13, 40, tzinfo=UTC)
        s = ny.supervise(now, [], [order(1, "buy_stop", self.t_place), order(2, "sell_stop", self.t_place)], self.day, CFG)
        self.assertEqual(s.actions, [])
        self.assertFalse(s.done)
        self.assertEqual(s.next_check_seconds, CFG.poll_seconds)

    def test_cancel_after_expiration(self) -> None:
        now = datetime(2026, 9, 28, 14, 2, 30, tzinfo=UTC)
        s = ny.supervise(now, [], [order(1, "buy_stop", self.t_place)], self.day, CFG)
        self.assertEqual([a.type for a in s.actions], ["cancel_order"])
        self.assertEqual(s.actions[0].order_id, 1)

    def test_oco_cancel_when_position_open(self) -> None:
        now = datetime(2026, 9, 28, 13, 45, tzinfo=UTC)
        s = ny.supervise(now, [pos(10, "buy", datetime(2026, 9, 28, 13, 44, 10, tzinfo=UTC))],
                         [order(2, "sell_stop", self.t_place)], self.day, CFG)
        self.assertEqual([(a.type, a.order_id) for a in s.actions], [("cancel_order", 2)])
        self.assertFalse(s.done)
        self.assertLessEqual(s.next_check_seconds, CFG.poll_seconds)

    def test_second_position_closed(self) -> None:
        now = datetime(2026, 9, 28, 13, 50, tzinfo=UTC)
        first = pos(10, "buy", datetime(2026, 9, 28, 13, 44, tzinfo=UTC))
        second = pos(11, "sell", datetime(2026, 9, 28, 13, 49, tzinfo=UTC), sl=4300.0, tp=4290.7)
        s = ny.supervise(now, [second, first], [], self.day, CFG)
        self.assertEqual([(a.type, a.position_id) for a in s.actions], [("close_position", 11)])
        self.assertEqual(s.state["primary"]["positionId"], 10)

    def test_time_stop(self) -> None:
        opened = datetime(2026, 9, 28, 13, 44, tzinfo=UTC)
        s_before = ny.supervise(opened + timedelta(minutes=59), [pos(10, "buy", opened)], [], self.day, CFG)
        self.assertEqual(s_before.actions, [])
        self.assertEqual(s_before.next_check_seconds, CFG.poll_seconds)
        s_after = ny.supervise(opened + timedelta(minutes=60), [pos(10, "buy", opened)], [], self.day, CFG)
        self.assertEqual([(a.type, a.position_id) for a in s_after.actions], [("close_position", 10)])
        self.assertIn("limite de temps", s_after.actions[0].reason)

    def test_done_states(self) -> None:
        s = ny.supervise(datetime(2026, 9, 28, 14, 5, tzinfo=UTC), [], [], self.day, CFG)
        self.assertTrue(s.done)
        s2 = ny.supervise(datetime(2026, 9, 28, 13, 50, tzinfo=UTC), [], [], self.day, CFG, trade_done=True)
        self.assertTrue(s2.done)
        s3 = ny.supervise(datetime(2026, 9, 28, 13, 50, tzinfo=UTC), [], [], self.day, CFG)
        self.assertFalse(s3.done)
        self.assertTrue(any("non placés" in w for w in s3.warnings))

    def test_foreign_positions_ignored(self) -> None:
        other = PositionState(direction="buy", volume_lots=0.1, price_open=1.1, symbol="EURUSD",
                              time_open=datetime(2026, 9, 28, 13, 40, tzinfo=UTC), position_id=99)
        s = ny.supervise(datetime(2026, 9, 28, 14, 5, tzinfo=UTC), [other], [], self.day, CFG)
        self.assertTrue(s.done)
        self.assertTrue(any("hors périmètre" in w for w in s.warnings))

    def test_trade_done_from_fills(self) -> None:
        fills = [
            {"symbol": "", "action": "dealbalance", "entry": "entryin", "profit": 10000, "time": "2026-09-28T11:00:00+00:00"},
            {"symbol": "XAUUSD", "action": "buy", "entry": "entryin", "profit": 0, "time": "2026-09-28T13:44:00+00:00"},
        ]
        self.assertFalse(ny.trade_done_from_fills(fills, self.day, CFG))
        fills.append({"symbol": "XAUUSD", "action": "sell", "entry": "entryout", "profit": 97.8, "time": "2026-09-28T13:50:00+00:00"})
        self.assertTrue(ny.trade_done_from_fills(fills, self.day, CFG))


class BacktestTests(unittest.TestCase):
    def day_bars(self, day: date, scenario: str) -> list[Candle]:
        t0 = ref_at(day)
        bars = [bar(t0 - timedelta(minutes=1), 4298, 4299, 4297.5, 4298.5), bar(t0, 4298.0, 4300.0, 4297.0, 4299.0)]
        if scenario == "tp":
            bars += [
                bar(t0 + timedelta(minutes=1), 4299.5, 4300.5, 4299.0, 4300.4),  # ask high 4300.66 ≥ 4300.26 → achat
                bar(t0 + timedelta(minutes=2), 4300.4, 4303.0, 4300.0, 4302.5),
                bar(t0 + timedelta(minutes=3), 4302.5, 4307.0, 4302.0, 4306.0),  # high ≥ TP 4306.78
            ]
            bars += flat_bars(t0 + timedelta(minutes=4), 70, 4303, 4304)
        elif scenario == "ambiguous_sl":
            bars += [bar(t0 + timedelta(minutes=1), 4298.0, 4300.5, 4296.5, 4297.0)]  # les deux niveaux, corps baissier → vente, SL touché
            bars += flat_bars(t0 + timedelta(minutes=2), 70, 4297, 4298)
        elif scenario == "none":
            bars += flat_bars(t0 + timedelta(minutes=1), 70, 4297.5, 4299.5)
        elif scenario == "time":
            bars += [bar(t0 + timedelta(minutes=1), 4299.5, 4300.5, 4299.0, 4300.4)]  # achat
            bars += flat_bars(t0 + timedelta(minutes=2), 75, 4299.0, 4303.0)  # ni SL (4297) ni TP (4306.78)
        return bars

    def test_tp_day_gives_two_r(self) -> None:
        day = date(2026, 9, 21)
        res = bt.simulate_day(day, self.day_bars(day, "tp"), 10_000, SPEC, CFG)
        self.assertEqual(res.status, "trade")
        self.assertEqual(res.side, "buy")
        self.assertEqual(res.exit_reason, "TP")
        self.assertAlmostEqual(res.entry, 4300.26)
        self.assertAlmostEqual(res.pnl, 97.8, places=2)  # 6.52 × 0.15 × 100
        self.assertAlmostEqual(res.r, 2.0, places=3)
        self.assertFalse(res.reversal_hit)

    def test_ambiguous_bar_takes_body_direction_and_sl(self) -> None:
        day = date(2026, 9, 22)
        res = bt.simulate_day(day, self.day_bars(day, "ambiguous_sl"), 10_000, SPEC, CFG)
        self.assertEqual(res.side, "sell")
        self.assertTrue(res.ambiguous)
        self.assertEqual(res.exit_reason, "SL")
        self.assertTrue(res.reversal_hit)  # niveau d'achat touché dans la même bougie
        self.assertGreater(res.reversal_cost, 0)
        # Le trade lui-même perd exactement 1 R ; le coût du second ordre s'ajoute.
        self.assertAlmostEqual(res.pnl + res.reversal_cost, -res.risk_money, places=2)
        self.assertLess(res.r, -1.0)

    def test_no_trigger(self) -> None:
        day = date(2026, 9, 23)
        res = bt.simulate_day(day, self.day_bars(day, "none"), 10_000, SPEC, CFG)
        self.assertEqual(res.status, "aucun")

    def test_time_stop_exit(self) -> None:
        day = date(2026, 9, 24)
        res = bt.simulate_day(day, self.day_bars(day, "time"), 10_000, SPEC, CFG)
        self.assertEqual(res.exit_reason, "temps")
        trigger = datetime.fromisoformat(res.trigger_time)
        self.assertEqual(datetime.fromisoformat(res.exit_time), trigger + timedelta(minutes=60))

    def test_missing_ref_bar_skipped(self) -> None:
        day = date(2026, 9, 25)
        bars = flat_bars(ref_at(day) + timedelta(minutes=1), 10, 4297, 4299)
        self.assertEqual(bt.simulate_day(day, bars, 10_000, SPEC, CFG).status, "saute")

    def test_run_backtest_summary(self) -> None:
        candles: list[Candle] = []
        for day, sc in ((date(2026, 9, 21), "tp"), (date(2026, 9, 22), "ambiguous_sl"), (date(2026, 9, 23), "none"), (date(2026, 9, 24), "time")):
            candles += self.day_bars(day, sc)
        summary, results = bt.run_backtest(candles, SPEC, CFG, 10_000)
        self.assertEqual(summary.days, 4)
        self.assertEqual(summary.trades, 3)
        self.assertEqual(summary.no_trigger, 1)
        self.assertEqual(summary.tp_exits, 1)
        self.assertEqual(summary.sl_exits, 1)
        self.assertEqual(summary.time_exits, 1)
        self.assertEqual(summary.ambiguous, 1)
        self.assertEqual(summary.reversal_events, 1)
        self.assertAlmostEqual(summary.final_equity, 10_000 + sum(r.pnl for r in results if r.status == "trade"), places=2)
        report = bt.format_report(summary, results, CFG, show_days=True)
        self.assertIn("BACKTEST", report)
        self.assertIn("2026-09-21", report)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, payload: object) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_plan_cli(self) -> None:
        day = date(2026, 9, 28)
        t0 = ref_at(day)
        candles = [bar(t0 - timedelta(minutes=1), 4298, 4299, 4297.5, 4298.5).to_dict(), bar(t0, 4298.0, 4300.0, 4297.0, 4299.0).to_dict()]
        c = self.write("candles.json", candles)
        a = self.write("account.json", {"cashBalance": 10000, "equity": 10000, "marginFree": 10000, "leverage": "1000", "currencyCode": "USD"})
        q = self.write("quote.json", {"symbol": "XAUUSD", "bid": 4298.9, "ask": 4299.06, "spread": 0.16, "point": 0.01, "tradeMode": "full", "tradeSessionOpen": True})
        s = self.write("symbol.json", {"symbol": "XAUUSD", "digits": 2, "contractSize": 100, "volumeMinLots": 0.01, "volumeMaxLots": 20, "volumeStepLots": 0.01, "stopsLevel": 1})
        out = os.path.join(self.dir, "plan.json")
        code = cli.main(["plan", "--ref-candle", c, "--account", a, "--quote", q, "--spec", s, "--now", "2026-09-28T13:32:05Z", "--out", out, "--quiet"])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIsNone(payload["plan"]["skipped"])
        self.assertEqual(len(payload["plan"]["orders"]), 2)
        self.assertEqual(payload["plan"]["orders"][0]["priceOrder"], 4300.26)
        self.assertEqual(payload["plan"]["orders"][1]["volumeLots"], 0.16)

    def test_plan_cli_missing_ref_bar_is_skipped_not_error(self) -> None:
        day = date(2026, 9, 28)
        c = self.write("candles.json", [bar(ref_at(day) - timedelta(minutes=1), 4298, 4299, 4297.5, 4298.5).to_dict()])
        a = self.write("account.json", {"cashBalance": 10000, "equity": 10000, "leverage": "1000"})
        out = os.path.join(self.dir, "plan.json")
        self.assertEqual(cli.main(["plan", "--ref-candle", c, "--account", a, "--day", "2026-09-28", "--out", out, "--quiet"]), 0)
        with open(out, encoding="utf-8") as handle:
            skipped = json.load(handle)["plan"]["skipped"]
        self.assertIsNotNone(skipped)
        self.assertIn("bougie", skipped)

    def test_supervise_cli(self) -> None:
        p = self.write("positions.json", [{"positionId": 10, "symbol": "XAUUSD", "direction": "buy", "volumeLots": 0.15, "priceOpen": 4300.26,
                                            "priceCurrent": 4301, "unrealisedPnl": 11, "swap": 0, "priceSL": 4297, "priceTP": 4306.78,
                                            "timeCreate": "2026-09-28T13:44:10+00:00"}])
        o = self.write("orders.json", [{"orderId": 2, "symbol": "XAUUSD", "type": "sell_stop", "state": "placed", "volumeInitialLots": 0.16,
                                         "priceOrder": 4296.9, "timeSetup": "2026-09-28T13:32:05+00:00", "timeExpiration": "2026-09-28T14:02:00+00:00"}])
        f = self.write("fills.json", [])
        out = os.path.join(self.dir, "actions.json")
        code = cli.main(["supervise", "--positions", p, "--orders", o, "--fills", f, "--day", "2026-09-28", "--now", "2026-09-28T13:45:00Z", "--out", out, "--quiet"])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["actions"], [{"type": "cancel_order", "reason": payload["actions"][0]["reason"], "orderId": 2}])
        self.assertFalse(payload["done"])

    def test_invalid_input_exit_code(self) -> None:
        self.assertEqual(cli.main(["supervise", "--positions", os.path.join(self.dir, "absent.json"), "--day", "2026-09-28"]), 2)


if __name__ == "__main__":
    unittest.main()
