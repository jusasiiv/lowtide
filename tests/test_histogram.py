import unittest

from lowtide.core import histogram as H, mempool_api as M
from .helpers import fixture


class HistogramTests(unittest.TestCase):

    def setUp(self):
        self.hist = M.parse_histogram(fixture('mempool'))

    def test_normalize_sorts_desc_and_drops_empty(self):
        h = H.normalize([[1.0, 10], [3.0, 5], [2.0, 0], ['x', 1], [0.5, 7]])
        self.assertEqual(h, [(3.0, 5), (1.0, 10), (0.5, 7)])

    def test_quantize(self):
        self.assertEqual(H.quantize_up(0.31), 0.4)
        self.assertEqual(H.quantize_up(0.4), 0.4)
        self.assertEqual(H.quantize(0.44), 0.4)
        self.assertEqual(H.quantize(0.45), 0.5)

    def test_depth_math(self):
        h = [(5.0, 100), (2.0, 300), (0.5, 600)]
        self.assertEqual(H.total_vsize(h), 1000)
        self.assertEqual(H.vsize_at_or_above(h, 2.0), 400)
        self.assertEqual(H.vsize_below(h, 2.0), 600)
        self.assertEqual(H.rate_at_depth(h, 50), 5.0)
        self.assertEqual(H.rate_at_depth(h, 150), 2.0)
        self.assertEqual(H.rate_at_depth(h, 5000), H.FLOOR_RATE)
        self.assertEqual(H.min_fee([(0.0, 5), (0.3, 1)]), 0.3)

    def test_fixture_sub1_pile(self):
        self.assertGreater(H.sub1_vsize(self.hist), 30_000_000)
        self.assertGreater(H.next_block_rate(self.hist), 1.0)

    def test_pile_jump_fixture(self):
        pj = H.pile_jump(self.hist)
        self.assertIsNotNone(pj)
        self.assertEqual(pj.rate, 0.4)
        self.assertGreater(pj.ahead_vb, 40_000_000)
        self.assertLessEqual(pj.behind_vb - pj.ceiling_behind_vb, 500_000)

    def test_pile_jump_none_without_pile(self):
        self.assertIsNone(H.pile_jump([(5.0, 100_000), (2.0, 200_000)]))

    def test_pile_jump_synthetic(self):
        # 30 vMB at 0.1, 5 vMB at 0.2, 0.1 vMB at 0.3, 2 vMB at 1.0, 1 vMB at 3
        h = [(3.0, 1_000_000), (1.0, 2_000_000), (0.3, 100_000), (0.2, 5_000_000), (0.1, 30_000_000)]
        pj = H.pile_jump(h)
        self.assertEqual(pj.rate, 0.3)  # 0.3 is within 0.5 vMB of paying 1.0
        self.assertEqual(pj.ahead_vb, 35_000_000)

    def test_bins_cover_everything(self):
        b = H.bins(self.hist)
        self.assertEqual(sum(v for _, _, v in b), H.total_vsize(self.hist))
        self.assertEqual(b[0][0], 0.0)
        self.assertAlmostEqual(b[1][0], 0.1)


if __name__ == '__main__':
    unittest.main()
