import unittest

from lowtide import core_mempool_api as M
from .helpers import fixture


class ApiTests(unittest.TestCase):

    def test_urls(self):
        self.assertEqual(M.url('https://mempool.space/', 'mempool'), 'https://mempool.space/api/mempool')
        self.assertEqual(M.url('https://mempool.space/testnet4', 'statistics', period='1w'),
                         'https://mempool.space/testnet4/api/v1/statistics/1w')
        self.assertEqual(M.url('http://umbrel.local:3006', 'tx_status', txid='ab'), 'http://umbrel.local:3006/api/tx/ab/status')

    def test_parsers_on_fixtures(self):
        self.assertEqual(len(M.STATISTICS_BANDS), 39)
        self.assertGreater(len(M.parse_histogram(fixture('mempool'))), 100)
        mb = M.parse_mempool_blocks(fixture('mempool_blocks'))
        self.assertEqual(len(mb), 8)
        self.assertEqual(len(mb[0]['fee_range']), 7)
        blocks = M.parse_blocks(fixture('blocks'))
        self.assertEqual(len(blocks), 15)
        self.assertTrue(blocks[0]['extras']['pool']['name'])
        st = M.parse_statistics(fixture('statistics_1w'))
        self.assertEqual(len(st), 2016)
        self.assertLess(st[0]['added'], st[-1]['added'])
        rec = M.parse_recommended(fixture('recommended'))
        self.assertIn('fastestFee', rec)

    def test_spike_levels(self):
        sp = M.spike_levels(fixture('feerates_all'))
        self.assertGreater(sp[2017], 500)
        self.assertGreater(sp[2024], 500)
        sp3 = M.spike_levels(fixture('feerates_3y'))
        self.assertGreater(sp3[2024], 1000)


if __name__ == '__main__':
    unittest.main()
