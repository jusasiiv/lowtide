import math
import unittest

from lowtide.core import eta as E, histogram as H, mempool_api as M
from .helpers import fixture


class EtaTests(unittest.TestCase):

    def test_erlang_quantiles(self):
        # one block: median ~ 416 s, 90% ~ 1382 s
        self.assertAlmostEqual(E.erlang_quantile(1, 600, 0.5), 600 * math.log(2), delta=1)
        self.assertAlmostEqual(E.erlang_quantile(1, 600, 0.9), 600 * math.log(10), delta=1)
        # more blocks take longer, higher quantile takes longer
        self.assertLess(E.erlang_quantile(3, 600, 0.5), E.erlang_quantile(6, 600, 0.5))
        self.assertLess(E.erlang_quantile(6, 600, 0.5), E.erlang_quantile(6, 600, 0.9))
        # 100 blocks: median close to 100 * 600
        self.assertAlmostEqual(E.erlang_quantile(100, 600, 0.5), 60000, delta=600)

    def test_blocks_needed(self):
        self.assertEqual(E.blocks_needed(500_000, 0), 1)
        self.assertEqual(E.blocks_needed(2_500_000, 0), 3)
        self.assertEqual(E.blocks_needed(1_000_000, 1000), 3)   # 0.4 vMB net per block
        self.assertIsNone(E.blocks_needed(1_000_000, 2000))     # arrivals exceed capacity

    def test_fraction_above(self):
        fr = [1, 2, 3, 4, 5, 6, 100]
        self.assertEqual(E.fraction_above(fr, 0.5), 1.0)
        self.assertEqual(E.fraction_above(fr, 200), 0.0)
        self.assertAlmostEqual(E.fraction_above(fr, 4), 0.5)
        self.assertAlmostEqual(E.fraction_above(fr, 3.5), 0.625)

    def test_eta_monotone_in_rate(self):
        hist = M.parse_histogram(fixture('mempool'))
        arr = E.make_arrival_fn(M.parse_blocks(fixture('blocks')))
        last = None
        for r in (3.0, 4.0, 5.0, 6.0, 8.0):
            e = E.eta_for_rate(hist, r, arr)
            self.assertIsNotNone(e.median_s)
            if last is not None:
                self.assertLessEqual(e.median_s, last + 1e-6)
            last = e.median_s

    def test_tide_fallback(self):
        hist = [(5.0, 400_000), (1.0, 5_000_000), (0.4, 1_000_000), (0.1, 40_000_000)]
        arr = lambda rate: 1700.0   # arrivals above capacity: steady state never drains
        e = E.eta_for_rate(hist, 0.4, arr)
        self.assertIsNone(e.median_s)
        e2 = E.eta_for_rate(hist, 0.4, arr, tide_fn=lambda r: (5 * 3600.0, 9 * 3600.0))
        self.assertEqual(e2.note, 'when the tide turns')
        self.assertAlmostEqual(e2.median_s, 5 * 3600 + 600)
        self.assertAlmostEqual(e2.p90_s, 9 * 3600 + 1800)

    def test_rate_for_deadline(self):
        hist = M.parse_histogram(fixture('mempool'))
        arr = E.make_arrival_fn(M.parse_blocks(fixture('blocks')))
        fast = E.rate_for_deadline(hist, 1800, arr)
        slow = E.rate_for_deadline(hist, 6 * 3600, arr)
        self.assertIsNotNone(fast)
        self.assertIsNotNone(slow)
        self.assertGreaterEqual(fast.rate, slow.rate)
        self.assertLessEqual(fast.p90_s, 1800)


if __name__ == '__main__':
    unittest.main()
