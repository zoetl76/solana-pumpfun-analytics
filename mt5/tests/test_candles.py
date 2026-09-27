"""Tests du chargement des bougies."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from mt5.engine.candles import (
    Candle,
    candle_close_time,
    candles_from_records,
    candles_to_json,
    infer_timeframe,
    load_candles,
    parse_time,
)


def _rec(time: str, o: float = 1.1, h: float = 1.2, l: float = 1.0, c: float = 1.15, spread: int = 6) -> dict:
    return {"time": time, "open": o, "high": h, "low": l, "close": c, "tickVolume": 10, "volume": 0, "spread": spread}


class ParseTimeTests(unittest.TestCase):
    def test_offset_and_z_suffix(self):
        a = parse_time("2026-09-24T00:00:00+00:00")
        b = parse_time("2026-09-24T00:00:00Z")
        self.assertEqual(a, b)
        self.assertEqual(a.tzinfo, timezone.utc)

    def test_naive_is_utc(self):
        self.assertEqual(parse_time("2026-09-24T01:00:00"), datetime(2026, 9, 24, 1, tzinfo=timezone.utc))

    def test_epoch_seconds(self):
        self.assertEqual(parse_time(0), datetime(1970, 1, 1, tzinfo=timezone.utc))

    def test_other_offset_converted(self):
        dt = parse_time("2026-09-24T02:00:00+02:00")
        self.assertEqual(dt, datetime(2026, 9, 24, 0, tzinfo=timezone.utc))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_time("pas une date")


class LoadCandlesTests(unittest.TestCase):
    def _write(self, payload) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_load_sorts_and_dedups(self):
        records = [
            _rec("2026-09-24T02:00:00+00:00", c=1.19),
            _rec("2026-09-24T00:00:00Z", c=1.1),
            _rec("2026-09-24T01:00:00+00:00", c=1.12),
            _rec("2026-09-24T01:00:00+00:00", c=1.15),  # doublon : la dernière gagne
        ]
        candles = load_candles(self._write(records))
        self.assertEqual(len(candles), 3)
        self.assertEqual([c.close for c in candles], [1.1, 1.15, 1.19])
        self.assertTrue(all(candles[i].time < candles[i + 1].time for i in range(2)))
        self.assertEqual(candles[0].spread_points, 6)
        self.assertEqual(candles[0].tick_volume, 10)

    def test_wrapped_dict_accepted(self):
        path = self._write({"candles": [_rec("2026-09-24T00:00:00Z")]})
        self.assertEqual(len(load_candles(path)), 1)

    def test_empty_list(self):
        self.assertEqual(load_candles(self._write([])), [])

    def test_invalid_low_gt_high(self):
        with self.assertRaises(ValueError):
            candles_from_records([_rec("2026-09-24T00:00:00Z", h=1.0, l=1.2)])

    def test_invalid_open_close_outside_range(self):
        with self.assertRaises(ValueError):
            candles_from_records([_rec("2026-09-24T00:00:00Z", o=1.0, h=1.1, l=0.9, c=5.0)])
        with self.assertRaises(ValueError):
            candles_from_records([_rec("2026-09-24T00:00:00Z", o=0.5, h=1.1, l=0.9, c=1.0)])
        # bornes incluses : open = low et close = high sont valides
        c = candles_from_records([_rec("2026-09-24T00:00:00Z", o=0.9, h=1.1, l=0.9, c=1.1)])[0]
        self.assertEqual((c.open, c.close), (0.9, 1.1))

    def test_missing_field(self):
        with self.assertRaises(ValueError):
            candles_from_records([{"time": "2026-09-24T00:00:00Z", "open": 1.0}])

    def test_roundtrip_json(self):
        candles = candles_from_records([_rec("2026-09-24T00:00:00Z"), _rec("2026-09-24T01:00:00Z")])
        again = candles_from_records(candles_to_json(candles))
        self.assertEqual(candles, again)
        self.assertEqual(candles_to_json(candles)[0]["spread"], 6)

    def test_sample_file(self):
        path = os.path.join(os.path.dirname(__file__), "..", "data", "EURUSD_H1_sample.json")
        if not os.path.exists(path):
            self.skipTest("fichier d'exemple absent")
        candles = load_candles(path)
        self.assertEqual(len(candles), 45)
        self.assertEqual(infer_timeframe(candles), timedelta(hours=1))


class TimeframeTests(unittest.TestCase):
    def test_infer_and_close_time(self):
        base = datetime(2026, 9, 24, tzinfo=timezone.utc)
        candles = [Candle(base + timedelta(hours=i), 1, 1, 1, 1) for i in range(5)]
        self.assertEqual(infer_timeframe(candles), timedelta(hours=1))
        self.assertEqual(candle_close_time(candles), base + timedelta(hours=5))

    def test_single_candle(self):
        c = Candle(datetime(2026, 9, 24, tzinfo=timezone.utc), 1, 1, 1, 1)
        self.assertIsNone(infer_timeframe([c]))
        self.assertEqual(candle_close_time([c]), c.time)


if __name__ == "__main__":
    unittest.main()
