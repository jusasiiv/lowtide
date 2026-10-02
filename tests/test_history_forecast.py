import math
import random
import unittest

from lowtide import core_history as HI, core_forecast as F, core_backtest as B, core_mempool_api as M, core_histogram as H
from .helpers import fixture


def synthetic_hourly(weeks=10, start=1_700_000_000, noise=0.15, seed=1):
    """Weekly pattern: cheap at night and on weekends, expensive weekday afternoons."""
    rnd = random.Random(seed)
    start = (start // 3600) * 3600
    pts = []
    for h in range(weeks * 168):
        ts = start + h * 3600
        slot = F.slot_of(ts)
        day, hour = divmod(slot, 24)
        base = 0.4 if day >= 5 else (0.5 if hour < 7 else (2.0 if 13 <= hour < 20 else 1.0))
        rate = base * math.exp(rnd.gauss(0, noise))
        pts.append(HI.HistoryPoint(ts, rate, rate < 1, 0, 0, 0, 0, 0, 0.1))
    return pts


class HistoryTests(unittest.TestCase):

    def test_rate_from_bands(self):
        v = [0.0] * len(M.STATISTICS_BANDS)
        v[0] = 40e6; v[1] = 0.5e6; v[2] = 0.3e6; v[5] = 0.1e6  # 40 vMB sub-1, 0.5 at 1-2, 0.3 at 2-3, 0.1 at 5-6
        rate, in0 = HI.rate_from_bands(v)
        self.assertTrue(in0)   # only 0.9 vMB above 1 sat/vB: the cut falls in the sub-1 band
        self.assertEqual(rate, 0.5)  # default resolver
        v[1] = 2e6
        rate, in0 = HI.rate_from_bands(v)
        self.assertFalse(in0)
        self.assertTrue(1.0 <= rate < 2.0)

    def test_sub1_resolver_uses_live_shape(self):
        hist = M.parse_histogram(fixture('mempool'))
        res = HI.sub1_resolver_from_hist(hist)
        self.assertGreaterEqual(res(100_000), 0.3)   # a little sub-1 needed -> the top of the pile
        self.assertEqual(res(60e6), H.FLOOR_RATE)    # more than the whole pile -> floor

    def test_merge_prefers_finer_series(self):
        coarse = [{'added': 0, 'vsizes': [1e6] + [0] * 38, 'vps': 1, 'min_fee': 0.1},
                  {'added': 7200, 'vsizes': [1e6] + [0] * 38, 'vps': 1, 'min_fee': 0.1}]
        fine = [{'added': 10, 'vsizes': [0, 5e6] + [0] * 37, 'vps': 2, 'min_fee': 0.1}]
        pts = HI.merge_series(coarse, fine)
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0].vps, 2)   # overridden by the fine point
        self.assertEqual(pts[1].vps, 1)

    def test_fixture_series(self):
        s1w, s1m, s3m = (M.parse_statistics(fixture(n)) for n in ('statistics_1w', 'statistics_1m', 'statistics_3m'))
        hist = M.parse_histogram(fixture('mempool'))
        hp = HI.hourly(HI.merge_series(s3m, s1m, s1w, sub1=HI.sub1_resolver_from_hist(hist)))
        self.assertGreater(len(hp), 24 * 50)  # 3m series is 2-hourly
        self.assertTrue(all(p.rate >= 0.1 for p in hp))


class ForecastTests(unittest.TestCase):

    def test_quantile(self):
        self.assertEqual(F.quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(F.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(F.quantile([7], 0.9), 7)

    def test_recovers_weekly_pattern(self):
        pts = synthetic_hourly()
        now = pts[-1].ts + 1800
        fc = F.build_forecast(pts, now, live_rate=None, data_as_of=pts[-1].ts, weeks=8)
        self.assertEqual(len(fc.horizon), 168)
        # weekend hours should be predicted cheap, weekday afternoons expensive
        wk_end = [p.median for p in fc.horizon if F.slot_of(p.ts) // 24 >= 5]
        wk_pm = [p.median for p in fc.horizon if F.slot_of(p.ts) // 24 < 5 and 13 <= F.slot_of(p.ts) % 24 < 20]
        self.assertLess(max(wk_end), min(wk_pm))
        self.assertTrue(fc.windows)
        self.assertTrue(all(w.hours >= 2 for w in fc.windows))
        self.assertTrue(all(p.lo <= p.median <= p.hi for p in fc.horizon))
        # weekends are inside low-tide windows
        covered = {h for w in fc.windows for h in range(w.start, w.end, 3600)}
        weekend_hours = [p.ts for p in fc.horizon if F.slot_of(p.ts) // 24 >= 5]
        self.assertGreater(sum(1 for h in weekend_hours if h in covered) / len(weekend_hours), 0.9)

    def test_live_blend_starts_at_live_rate(self):
        pts = synthetic_hourly()
        now = pts[-1].ts + 1800
        fc = F.build_forecast(pts, now, live_rate=7.0, data_as_of=pts[-1].ts, weeks=8)
        self.assertEqual(fc.horizon[0].median, 7.0)
        self.assertLess(fc.horizon[12].median, 7.0)

    def test_time_until_rate_clears(self):
        pts = synthetic_hourly()
        now = pts[-1].ts + 1800
        fc = F.build_forecast(pts, now, live_rate=3.0, data_as_of=pts[-1].ts, weeks=8)
        med, hi = fc.time_until_rate_clears(0.6, now)
        self.assertIsNotNone(med)
        self.assertGreater(med, 0)
        self.assertTrue(hi is None or hi >= med)

    def test_roundtrip_dict(self):
        pts = synthetic_hourly(weeks=4)
        fc = F.build_forecast(pts, pts[-1].ts, 1.0, data_as_of=pts[-1].ts, weeks=4)
        fc2 = F.Forecast.from_dict(fc.to_dict())
        self.assertEqual(fc2.windows, fc.windows)
        self.assertEqual(fc2.horizon[5], fc.horizon[5])


class BacktestTests(unittest.TestCase):

    def test_synthetic_beats_baseline(self):
        r = B.run_backtest(synthetic_hourly(weeks=14), test_weeks=4, train_weeks=8)
        self.assertEqual(len(r['weeks']), 4)
        self.assertGreater(r['hit_rate_vs_median'], 0.9)
        self.assertGreater(r['hit_rate_vs_median'], r['baseline_vs_median'])
        self.assertIn('%', B.headline(r))

    def test_fixture_backtest_beats_baseline(self):
        s1w, s1m, s3m = (M.parse_statistics(fixture(n)) for n in ('statistics_1w', 'statistics_1m', 'statistics_3m'))
        hist = M.parse_histogram(fixture('mempool'))
        hp = HI.hourly(HI.merge_series(s3m, s1m, s1w, sub1=HI.sub1_resolver_from_hist(hist)))
        r = B.run_backtest(hp)
        self.assertGreater(r['hit_rate_vs_median'], r['baseline_vs_median'] + 0.1)


if __name__ == '__main__':
    unittest.main()
