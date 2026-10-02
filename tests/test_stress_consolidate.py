import unittest

from lowtide import core_stress as S, core_consolidate as C


def coin(i, value, vsize=68.0, label='', txid=None, confirmed=True, frozen=False):
    return S.Coin(outpoint=f'{txid or "t%02d" % i}:{i}', value=value, vsize=vsize, label=label,
                  txid=txid or 't%02d' % i, confirmed=confirmed, frozen=frozen)


class StressTests(unittest.TestCase):

    def test_tech_check(self):
        tc = S.tech_check('p2pkh')
        self.assertFalse(tc.ok)
        self.assertAlmostEqual(tc.saving_pct, 54.05, delta=0.1)
        self.assertIn('native segwit', tc.message)
        self.assertTrue(S.tech_check('p2wpkh').ok)
        self.assertFalse(S.tech_check('p2wpkh-p2sh').ok)
        ms = S.tech_check('p2sh', 2, 3)
        self.assertFalse(ms.ok)
        self.assertGreater(ms.input_vb, 250)
        self.assertLess(ms.best_vb, 120)
        self.assertTrue(S.tech_check('p2wsh', 2, 3).ok)

    def test_stress_rows(self):
        coins = [coin(i, 1000) for i in range(20)]  # 20 native segwit coins
        rows = S.stress_table(coins, S.default_scenarios(4.0, 0.2, {2024: 1190.0}, custom_rate=100.0))
        self.assertEqual([r.scenario.kind for r in rows], ['now', 'lowtide', 'spike', 'spike' if False else 'custom'][:4])
        low = rows[1]
        self.assertAlmostEqual(low.vsize, 10.5 + 20 * 68 + 31)
        self.assertEqual(low.fee, 281)     # ~280 sats at 0.2 sat/vB
        self.assertEqual(low.uneconomical, [])
        spike = rows[2]
        self.assertGreater(spike.fee, 140_000)
        self.assertEqual(len(spike.uneconomical), 20)   # 68 vB * 1190 > 1000 sats each
        self.assertGreater(spike.fee_share, 1.0)

    def test_uneconomical_threshold(self):
        coins = [coin(0, 500), coin(1, 50_000)]
        row = S.stress_row(coins, S.Scenario('x', 10.0, 'custom'))
        self.assertEqual([c.value for c in row.uneconomical], [500])


class ConsolidateTests(unittest.TestCase):

    def test_grouping_by_label(self):
        coins = [coin(0, 1000, label='exchange'), coin(1, 2000, label='exchange', txid='t00'),
                 coin(2, 3000, label='shop'), coin(3, 4000), coin(4, 5000, frozen=True), coin(5, 6000, confirmed=False)]
        groups = C.group_coins(coins)
        self.assertEqual([g.title for g in groups], ['exchange', 'shop', 'Unlabeled'])
        self.assertEqual(groups[0].sources, 1)
        self.assertEqual(sum(len(g.coins) for g in groups), 4)  # frozen and unconfirmed skipped

    def test_plans_separate_vs_merged(self):
        coins = [coin(0, 1000, label='a'), coin(1, 2000, label='a'), coin(2, 3000, label='b'), coin(3, 4000, label='b')]
        groups = C.group_coins(coins)
        sep = C.make_plan(groups, 0.2, merge=False)
        self.assertEqual(len(sep), 2)
        self.assertFalse(any(p.links_groups for p in sep))
        merged = C.make_plan(groups, 0.2, merge=True)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].links_groups)
        self.assertEqual(len(merged[0].coins), 4)
        self.assertEqual(merged[0].output_value, 10000 - merged[0].fee)
        self.assertIn('publicly link', C.linking_warning(groups))

    def test_single_coin_group_has_no_plan(self):
        self.assertEqual(C.make_plan(C.group_coins([coin(0, 1000)]), 0.2, merge=False), [])

    def test_contrast_text(self):
        t = C.contrast(1400, 0.2, 100)
        self.assertIn('280 sats', t)
        self.assertIn('140,000 sats', t)


if __name__ == '__main__':
    unittest.main()
