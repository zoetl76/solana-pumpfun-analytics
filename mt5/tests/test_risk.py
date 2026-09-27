"""Tests de la gestion du risque."""

import unittest
from datetime import date, datetime, timezone

from mt5.engine.risk import (
    check_margin,
    compute_lots,
    guard_daily_loss,
    guard_daily_loss_amount,
    guard_friday_cutoff,
    guard_max_positions,
    guard_session,
    guard_spread,
    guard_trade_mode,
    guard_trading_hours,
    in_trading_hours,
    is_non_trading_deal,
    realized_pnl_today,
    round_lots_down,
    step_decimals,
    validate_stops,
)
from mt5.engine.strategy import PositionState, SymbolSpec

SPEC = SymbolSpec.default_fx("EURUSD")


class LotSizingTests(unittest.TestCase):
    def test_exact(self):
        # 1 % de 10 000 = 100 ; 50 pips = 0.0050 × 100 000 = 500 par lot → 0.20 lot
        res = compute_lots(10_000, 1.0, 1.10000, 1.09500, SPEC)
        self.assertTrue(res.ok)
        self.assertAlmostEqual(res.lots, 0.20)
        self.assertAlmostEqual(res.risk_money, 100.0)

    def test_rounds_down_to_step(self):
        # 100 / (0.0033 × 100 000 = 330) = 0.30303 → 0.30 (jamais 0.31)
        res = compute_lots(10_000, 1.0, 1.10000, 1.09670, SPEC)
        self.assertAlmostEqual(res.lots, 0.30)
        self.assertEqual(round_lots_down(0.30303, 0.01), 0.30)
        self.assertEqual(round_lots_down(0.2999999, 0.01), 0.29)
        self.assertEqual(round_lots_down(0.3, 0.01), 0.30)  # tolérance flottante
        self.assertEqual(round_lots_down(1.26, 0.1), 1.2)
        self.assertEqual(round_lots_down(7.9, 1.0), 7.0)

    def test_step_decimals(self):
        self.assertEqual(step_decimals(0.01), 2)
        self.assertEqual(step_decimals(0.1), 1)
        self.assertEqual(step_decimals(1.0), 0)

    def test_clamp_min_with_warning(self):
        res = compute_lots(100, 1.0, 1.10000, 1.09500, SPEC)  # 1 / 500 = 0.002 → min 0.01
        self.assertAlmostEqual(res.lots, 0.01)
        self.assertTrue(res.warnings)

    def test_clamp_max(self):
        res = compute_lots(100_000_000, 1.0, 1.10000, 1.09990, SPEC)
        self.assertAlmostEqual(res.lots, SPEC.volume_max)

    def test_zero_or_none_sl(self):
        self.assertEqual(compute_lots(10_000, 1.0, 1.1, 1.1, SPEC).lots, 0.0)
        self.assertIsNotNone(compute_lots(10_000, 1.0, 1.1, 1.1, SPEC).reason)
        self.assertEqual(compute_lots(10_000, 1.0, 1.1, None, SPEC).lots, 0.0)

    def test_below_stops_level(self):
        spec = SymbolSpec.default_fx()
        spec.stops_level = 10
        res = compute_lots(10_000, 1.0, 1.10000, 1.09995, spec)  # 5 points < 10
        self.assertEqual(res.lots, 0.0)
        self.assertIn("stopsLevel", res.reason)

    def test_rate_applied(self):
        res = compute_lots(10_000, 1.0, 1.10000, 1.09500, SPEC, rate=2.0)
        self.assertAlmostEqual(res.lots, 0.10)


class MarginTests(unittest.TestCase):
    def test_margin_cap(self):
        # 1 lot × 100 000 × 1.1 / 1000 = 110 ; plafond 0.8 × 100 = 80 → refus, max 0.72 lot
        chk = check_margin(1.0, 1.1, 100_000, 1000, 100, 0.8)
        self.assertFalse(chk.ok)
        self.assertAlmostEqual(chk.margin_required, 110.0)
        self.assertAlmostEqual(chk.margin_allowed, 80.0)
        self.assertAlmostEqual(chk.max_lots, 0.72)

    def test_margin_ok(self):
        chk = check_margin(0.1, 1.1, 100_000, 1000, 10_000, 0.8)
        self.assertTrue(chk.ok)

    def test_margin_accepts_connector_string_leverage(self):
        chk = check_margin(0.1, 1.1, 100_000, "1000", 10_000)
        self.assertTrue(chk.ok)
        self.assertAlmostEqual(chk.margin_required, 11.0)
        self.assertAlmostEqual(check_margin(0.1, 1.1, 100_000, "1:500", 10_000).margin_required, 22.0)
        # levier illisible ou nul → défaut 100, jamais d'exception
        self.assertAlmostEqual(check_margin(0.1, 1.1, 100_000, "n/a", 10_000).margin_required, 110.0)
        self.assertAlmostEqual(check_margin(0.1, 1.1, 100_000, 0, 10_000).margin_required, 110.0)


class GuardTests(unittest.TestCase):
    def test_daily_loss_from_fills(self):
        today = date(2026, 9, 25)
        fills = [
            {"profit": -150.0, "time": "2026-09-25T09:00:00+00:00"},
            {"realisedPnl": -200.0, "dealTime": "2026-09-25T10:00:00Z"},
            {"pnl": 500.0, "timeCreate": "2026-09-24T10:00:00Z"},  # hier : ignoré
            {"sans": "rien"},  # illisible
            "chaine",  # illisible
        ]
        total, counted, ignored, non_trading = realized_pnl_today(fills, today)
        self.assertAlmostEqual(total, -350.0)
        self.assertEqual(counted, 2)
        self.assertEqual(ignored, 2)
        self.assertEqual(non_trading, 0)
        blocked, warnings = guard_daily_loss(fills, 10_000, 3.0, today)
        self.assertEqual(blocked[0].rule, "max_daily_loss")
        self.assertTrue(any("illisible" in w for w in warnings))
        self.assertEqual(guard_daily_loss(fills, 10_000, 5.0, today)[0], [])
        self.assertEqual(guard_daily_loss(None, 10_000, 3.0, today)[0], [])

    def test_daily_loss_ignores_balance_deals(self):
        today = date(2026, 9, 25)
        # Forme réelle d'Axi (dépôt sur la démo) : action « dealbalance », symbole vide, volume 0.
        deposit = {"dealId": 89842889, "action": "dealbalance", "entry": "entryin", "symbol": "", "volumeLots": 0,
                   "profit": 10000, "swap": 0, "commission": 0, "time": "2026-09-25T11:53:32.908+00:00"}
        loss = {"action": "dealsell", "entry": "entryout", "symbol": "EURUSD", "volumeLots": 0.5,
                "profit": -500, "commission": -7, "time": "2026-09-25T13:00:00+00:00"}
        self.assertTrue(is_non_trading_deal(deposit))
        self.assertFalse(is_non_trading_deal(loss))
        total, counted, ignored, non_trading = realized_pnl_today([deposit, loss], today)
        self.assertAlmostEqual(total, -507.0)
        self.assertEqual((counted, ignored, non_trading), (1, 0, 1))
        blocked, warnings = guard_daily_loss([deposit, loss], 9_500, 3.0, today)
        self.assertEqual([b.rule for b in blocked], ["max_daily_loss"])
        self.assertTrue(any("solde" in w for w in warnings))
        # Forme générique du schéma du connecteur : action « balance » / entry « in ».
        generic = [
            {"action": "balance", "entry": "in", "profit": 10000.0, "time": "2026-09-25T08:00:00Z"},
            {"action": "sell", "entry": "out", "profit": -400.0, "time": "2026-09-25T09:00:00Z"},
        ]
        self.assertAlmostEqual(realized_pnl_today(generic, today)[0], -400.0)
        self.assertEqual(guard_daily_loss(generic, 10_000, 3.0, today)[0][0].rule, "max_daily_loss")
        # Un retrait ne bloque pas non plus à tort.
        withdrawal = [{"action": "dealbalance", "symbol": "", "volumeLots": 0, "profit": -5000, "time": "2026-09-25T08:00:00Z"}]
        self.assertEqual(guard_daily_loss(withdrawal, 5_000, 3.0, today)[0], [])

    def test_daily_loss_reference_is_day_start_balance(self):
        # Solde actuel 9 705 après une perte de 295 : la limite est 3 % de 10 000 (début de journée) = 300 → pas bloqué.
        today = date(2026, 9, 25)
        fills = [{"profit": -295.0, "time": "2026-09-25T09:00:00Z"}]
        self.assertEqual(guard_daily_loss(fills, 10_000 - 295, 3.0, today)[0], [])
        fills = [{"profit": -300.0, "time": "2026-09-25T09:00:00Z"}]
        self.assertEqual(len(guard_daily_loss(fills, 10_000 - 300, 3.0, today)[0]), 1)

    def test_validate_stops_against_bid_ask(self):
        spec = SymbolSpec.default_fx()
        bid, ask = 1.10000, 1.10006
        self.assertEqual(validate_stops("buy", 1.09500, 1.11000, bid, ask, spec), [])
        self.assertEqual(validate_stops("sell", 1.10500, 1.09000, bid, ask, spec), [])
        self.assertEqual(validate_stops("buy", None, None, bid, ask, spec), [])
        # SL d'achat entre bid et ask : sous le prix d'entrée mais au-dessus du bid → invalide
        self.assertEqual([b.rule for b in validate_stops("buy", 1.10003, 1.11, bid, ask, spec)], ["invalid_stops"])
        # TP d'achat sous le bid, SL de vente sous le ask, TP de vente au-dessus du ask
        self.assertEqual(len(validate_stops("buy", 1.095, 1.09999, bid, ask, spec)), 1)
        self.assertEqual(len(validate_stops("sell", 1.10005, 1.09, bid, ask, spec)), 1)
        self.assertEqual(len(validate_stops("sell", 1.105, 1.10007, bid, ask, spec)), 1)
        # stopsLevel : distance minimale mesurée depuis le bid (achat) / ask (vente)
        spec.stops_level = 10
        self.assertEqual(len(validate_stops("buy", 1.095, 1.10008, bid, ask, spec)), 1)  # TP à 8 points du bid
        self.assertEqual(validate_stops("buy", 1.095, 1.10010, bid, ask, spec), [])      # exactement 10 points
        self.assertEqual(len(validate_stops("sell", 1.10015, 1.09, bid, ask, spec)), 1)  # SL à 9 points du ask
        self.assertEqual(validate_stops("sell", 1.10016, 1.09, bid, ask, spec), [])

    def test_daily_loss_amount(self):
        self.assertEqual(guard_daily_loss_amount(-299.0, 10_000, 3.0), [])
        self.assertEqual(len(guard_daily_loss_amount(-300.0, 10_000, 3.0)), 1)
        self.assertEqual(guard_daily_loss_amount(-1000.0, 10_000, None), [])

    def test_max_positions(self):
        pos = PositionState("buy", 0.1, 1.1, symbol="EURUSD")
        self.assertEqual(len(guard_max_positions([pos], "EURUSD", 1)), 1)
        self.assertEqual(guard_max_positions([pos], "EURUSD", 2), [])
        self.assertEqual(guard_max_positions([], "EURUSD", 1), [])
        other = PositionState("buy", 0.1, 1.1, symbol="GBPUSD")
        self.assertEqual(guard_max_positions([other], "EURUSD", 1), [])

    def test_spread(self):
        self.assertEqual(guard_spread(6, 15), [])
        self.assertEqual(guard_spread(16, 15)[0].rule, "max_spread")
        self.assertEqual(guard_spread(50, None), [])
        self.assertEqual(guard_spread(None, 15), [])

    def test_trading_hours(self):
        t = datetime(2026, 9, 23, 6, 30, tzinfo=timezone.utc)  # mercredi 06:30
        self.assertEqual(guard_trading_hours(t, (7, 21))[0].rule, "trading_hours")
        self.assertEqual(guard_trading_hours(t.replace(hour=7), (7, 21)), [])
        self.assertEqual(guard_trading_hours(t.replace(hour=20), (7, 21)), [])
        self.assertEqual(len(guard_trading_hours(t.replace(hour=21), (7, 21))), 1)
        self.assertEqual(guard_trading_hours(t, None), [])
        # plage de nuit 22h-6h
        self.assertTrue(in_trading_hours(t.replace(hour=23), (22, 6)))
        self.assertTrue(in_trading_hours(t.replace(hour=2), (22, 6)))
        self.assertFalse(in_trading_hours(t.replace(hour=12), (22, 6)))

    def test_friday_cutoff_and_weekend(self):
        friday_19 = datetime(2026, 9, 25, 19, 30, tzinfo=timezone.utc)
        friday_20 = datetime(2026, 9, 25, 20, 3, tzinfo=timezone.utc)
        saturday = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        sunday_20 = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
        sunday_22 = datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(guard_friday_cutoff(friday_19, 20), [])
        self.assertEqual(guard_friday_cutoff(friday_20, 20)[0].rule, "friday_cutoff")
        self.assertEqual(guard_friday_cutoff(friday_20, None), [])
        self.assertEqual(guard_friday_cutoff(saturday, 20)[0].rule, "weekend")
        self.assertEqual(guard_friday_cutoff(sunday_20, 20)[0].rule, "weekend")
        self.assertEqual(guard_friday_cutoff(sunday_22, 20), [])
        # cutoff None = règle entièrement désactivée, week-end compris (symboles 24/7)
        self.assertEqual(guard_friday_cutoff(saturday, None), [])
        self.assertEqual(guard_friday_cutoff(sunday_20, None), [])

    def test_trade_mode(self):
        self.assertEqual(guard_trade_mode("full", "buy"), [])
        self.assertEqual(guard_trade_mode(None, "sell"), [])
        self.assertEqual(guard_trade_mode("longonly", "sell")[0].rule, "trade_mode")
        self.assertEqual(guard_trade_mode("longonly", "buy"), [])
        self.assertEqual(guard_trade_mode("shortonly", "buy")[0].rule, "trade_mode")
        self.assertEqual(guard_trade_mode("shortonly", "sell"), [])
        self.assertEqual(len(guard_trade_mode("closeonly", "buy")), 1)
        self.assertEqual(len(guard_trade_mode("disabled", "sell")), 1)

    def test_session(self):
        self.assertEqual(guard_session(True), [])
        self.assertEqual(guard_session(None), [])
        blocked = guard_session(False, "2026-09-27T21:01:00+00:00")
        self.assertEqual(blocked[0].rule, "session_closed")
        self.assertIn("21:01", blocked[0].detail)


if __name__ == "__main__":
    unittest.main()
