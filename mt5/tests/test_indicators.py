"""Tests des indicateurs contre des valeurs calculées à la main."""

import math
import unittest

from mt5.engine.indicators import (
    atr,
    bollinger,
    crossover,
    crossunder,
    ema,
    highest,
    lowest,
    macd,
    rsi,
    sma,
    true_range,
)


class SmaEmaTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_sma_period_one(self):
        self.assertEqual(sma([1.5, 2.5], 1), [1.5, 2.5])

    def test_ema_hand_computed(self):
        # alpha = 2/(3+1) = 0.5 ; amorce = SMA(3) = 2 ; puis 0.5*4+0.5*2 = 3 ; 0.5*5+0.5*3 = 4
        out = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_ema_alignment_and_warmup(self):
        out = ema([float(i) for i in range(50)], 21)
        self.assertEqual(len(out), 50)
        self.assertTrue(all(v is None for v in out[:20]))
        self.assertTrue(all(v is not None for v in out[20:]))

    def test_none_input_propagates(self):
        out = sma([1.0, None, 3.0, 4.0], 2)
        self.assertIsNone(out[1])
        self.assertIsNone(out[2])
        self.assertAlmostEqual(out[3], 3.5)


class RsiTests(unittest.TestCase):
    def test_hand_computed_period_2(self):
        # variations : +1, +1, -1
        out = rsi([1, 2, 3, 2], 2)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 100.0)
        # gain moyen = (1*1 + 0)/2 = 0.5 ; perte moyenne = (0*1 + 1)/2 = 0.5 → RS = 1 → 50
        self.assertAlmostEqual(out[3], 50.0)

    def test_monotonic(self):
        up = rsi([float(i) for i in range(20)], 14)
        down = rsi([float(20 - i) for i in range(20)], 14)
        self.assertAlmostEqual(up[-1], 100.0)
        self.assertAlmostEqual(down[-1], 0.0)

    def test_short_series(self):
        self.assertEqual(rsi([1, 2, 3], 14), [None, None, None])


class AtrTests(unittest.TestCase):
    def test_true_range(self):
        tr = true_range([10, 12, 11], [8, 9, 9], [9, 11, 10])
        self.assertEqual(tr, [2.0, 3.0, 2.0])

    def test_atr_wilder_hand_computed(self):
        # TR = [2, 3, 2] ; ATR(2)[1] = 2.5 ; ATR[2] = (2.5*1 + 2)/2 = 2.25
        out = atr([10, 12, 11], [8, 9, 9], [9, 11, 10], 2)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], 2.5)
        self.assertAlmostEqual(out[2], 2.25)


class BollingerMacdTests(unittest.TestCase):
    def test_bollinger(self):
        mid, upper, lower = bollinger([1, 2, 3], 3, 2.0)
        sd = math.sqrt(2.0 / 3.0)
        self.assertAlmostEqual(mid[2], 2.0)
        self.assertAlmostEqual(upper[2], 2.0 + 2 * sd)
        self.assertAlmostEqual(lower[2], 2.0 - 2 * sd)
        self.assertEqual(upper[:2], [None, None])

    def test_macd_alignment(self):
        values = [math.sin(i / 3.0) * 10 + 100 for i in range(60)]
        line, sig, hist = macd(values, 12, 26, 9)
        self.assertEqual((len(line), len(sig), len(hist)), (60, 60, 60))
        self.assertIsNone(line[24])
        self.assertIsNotNone(line[25])
        first_sig = next(i for i, v in enumerate(sig) if v is not None)
        self.assertEqual(first_sig, 25 + 8)
        for m, s, h in zip(line, sig, hist):
            if m is not None and s is not None:
                self.assertAlmostEqual(h, m - s)
            else:
                self.assertIsNone(h)
        # cohérence avec les EMA
        ef, es = ema(values, 12), ema(values, 26)
        self.assertAlmostEqual(line[40], ef[40] - es[40])


class HighLowCrossTests(unittest.TestCase):
    def test_highest_lowest(self):
        self.assertEqual(highest([1, 3, 2, 5, 4], 3), [None, None, 3.0, 5.0, 5.0])
        self.assertEqual(lowest([1, 3, 2, 5, 4], 3), [None, None, 1.0, 2.0, 2.0])

    def test_crossover_crossunder(self):
        a = [1.0, 2.0, 3.0, 2.0, 1.0]
        b = [2.0, 2.0, 2.0, 2.0, 2.0]
        self.assertFalse(crossover(a, b, 0))
        self.assertFalse(crossover(a, b, 1))  # égalité puis au-dessus à i=2
        self.assertTrue(crossover(a, b, 2))
        self.assertFalse(crossover(a, b, 3))
        self.assertTrue(crossunder(a, b, 4))
        self.assertFalse(crossunder(a, b, 2))

    def test_cross_none_safe(self):
        a = [None, 3.0]
        b = [2.0, 2.0]
        self.assertFalse(crossover(a, b, 1))
        self.assertFalse(crossover(a, b, 5))


if __name__ == "__main__":
    unittest.main()
