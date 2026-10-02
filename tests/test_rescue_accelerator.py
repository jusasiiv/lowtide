import unittest

from lowtide import core_rescue as R, core_accelerator as A
from lowtide.core_eta import Eta
from .helpers import fixture


def eta_for(rate):
    # faster for higher rates
    med = max(600.0, 3600.0 * 4 / max(rate, 0.1))
    return Eta(rate, 0, 0.0, 1, med, med * 1.5, '')


class RescueTests(unittest.TestCase):

    def facts(self, **kw):
        base = dict(txid='ab' * 32, vsize=140, fee=28, age_s=3600.0, can_bump=True, can_cpfp=True, script_type='p2wpkh',
                    has_lightning_sats=0, incoming=False, relay_rate=0.1)
        base.update(kw)
        return R.TxFacts(**base)

    def test_floor_tx_small_rbf_is_cheapest(self):
        f = self.facts()  # 0.2 sat/vB
        opts = R.compare(f, eta_fn=eta_for, next_block_rate=4.0, pile_rate=0.4, deadline_rate=2.0, mempool_min_fee=0.1,
                         accel_total=76_000)
        by = {o.key: o for o in opts}
        self.assertEqual(by['small_rbf'].new_rate, 0.4)
        self.assertEqual(by['small_rbf'].cost_sats, 28)          # 0.4*140 - 28
        self.assertEqual(by['rbf'].cost_sats, 252)               # 2.0*140 - 28
        self.assertTrue(by['cpfp'].available)
        self.assertEqual(by['accelerate'].cost_sats, 76_000)
        rec = R.recommend(opts, deadline_s=48 * 3600)
        self.assertEqual(rec.option.key, 'wait')                 # waiting (p90 30h) meets a 48h deadline
        rec2 = R.recommend(opts, deadline_s=24 * 3600)
        self.assertEqual(rec2.option.key, 'small_rbf')          # 28 sats beats everything else that meets 24h
        rec3 = R.recommend(opts, deadline_s=6 * 3600)
        self.assertEqual(rec3.option.key, 'rbf')                # 252 sats; CPFP 472, accelerator 76k

    def test_incoming_tx_only_wait_or_accelerate(self):
        f = self.facts(can_bump=False, can_cpfp=False, incoming=True)
        opts = R.compare(f, eta_fn=eta_for, next_block_rate=4.0, pile_rate=0.4, deadline_rate=2.0, mempool_min_fee=0.1, accel_total=76_000)
        by = {o.key: o for o in opts}
        self.assertFalse(by['small_rbf'].available)
        self.assertIn('not ours', by['small_rbf'].reason)
        self.assertFalse(by['cpfp'].available)
        rec = R.recommend(opts, deadline_s=1800)
        self.assertEqual(rec.option.key, 'accelerate')

    def test_eviction_and_expiry_notes(self):
        f = self.facts(fee=14)  # 0.1 sat/vB
        opts = R.compare(f, eta_fn=eta_for, next_block_rate=4.0, pile_rate=0.4, deadline_rate=None, mempool_min_fee=0.2, accel_total=None, accel_reason='testnet')
        self.assertIn('dropping', opts[0].reason)
        f2 = self.facts(age_s=13 * 86400)
        opts2 = R.compare(f2, eta_fn=eta_for, next_block_rate=4.0, pile_rate=0.4, deadline_rate=None, mempool_min_fee=0.1, accel_total=None)
        self.assertIn('expiry', opts2[0].reason)
        self.assertFalse({o.key: o for o in opts2}['accelerate'].available)

    def test_min_bump_respects_relay(self):
        f = self.facts(fee=56, relay_rate=1.0)  # 0.4 sat/vB on a 1 sat/vB relay server
        opts = R.compare(f, eta_fn=eta_for, next_block_rate=1.0, pile_rate=0.4, deadline_rate=None, mempool_min_fee=0.1, accel_total=None)
        by = {o.key: o for o in opts}
        self.assertGreaterEqual(by['rbf'].new_rate, 1.4)


class AcceleratorTests(unittest.TestCase):

    def test_parse_estimate_fixture(self):
        est = A.parse_estimate('x', fixture('accelerator_estimate'))
        self.assertEqual(est.base_fee, 75_000)
        self.assertEqual(len(est.options), 3)
        self.assertEqual(est.total(est.options[0]), est.options[0] + 75_000 + est.vsize_fee)
        self.assertEqual(est.bitcoin_min, 1000)
        self.assertFalse(est.unavailable)

    def test_parse_history_statuses(self):
        hist = fixture('accelerator_history')
        st = A.parse_status(hist[0]['txid'], hist)
        self.assertIsNotNone(st)
        self.assertEqual(st.status, hist[0]['status'].lower())
        self.assertIsNone(A.parse_status('nope', hist))
        done = [x for x in hist if x['status'].startswith('completed')]
        if done:
            self.assertTrue(A.parse_status(done[0]['txid'], hist).done)
        live = {'txid': 'ab', 'status': 'completed', 'blockHeight': 969463, 'feeDelta': 8007, 'bidBoost': 500, 'canceled': 0}
        s2 = A.parse_status('ab', live)
        self.assertTrue(s2.done and not s2.failed and s2.block_height == 969463)
        s3 = A.parse_status('ab', {'txid': 'ab', 'status': 'failed', 'canceled': 1})
        self.assertTrue(s3.failed)

    def test_bolt11_amount(self):
        self.assertEqual(A.bolt11_amount_sat('lnbc770u1pjexample'), 77_000)
        self.assertEqual(A.bolt11_amount_sat('lnbc1m1pjexample'), 100_000)
        self.assertEqual(A.bolt11_amount_sat('lnbc10n1pjexample'), 1)
        self.assertIsNone(A.bolt11_amount_sat('lnbc1pjexample'))

    def test_find_bolt11_in_nested(self):
        d = {'data': {'paymentMethods': [{'BOLT11': 'lnbc770u1' + 'q' * 60}]}}
        inv = A.parse_invoice('id', d)
        self.assertTrue(inv.bolt11.startswith('lnbc770u1'))

    def test_client_flow_with_fake_transport(self):
        calls = []
        def post(url, body):
            calls.append(('post', url, body))
            if url.endswith('/accelerator/estimate'):
                return fixture('accelerator_estimate')
            if url.endswith('/accelerator/invoice'):
                return {'btcpayInvoiceId': 'inv123'}
        def get(url):
            calls.append(('get', url))
            if 'payments/bitcoin/check' in url:
                return None  # 204 while unpaid
            if 'payments/bitcoin/invoice' in url:
                return {'btcpayInvoiceId': 'inv123', 'btcDue': 0.00077, 'addresses': {'BTC_LightningLike': 'lnbc770u1' + 'q' * 60}}
            if url.endswith('/accelerations/history'):
                return fixture('accelerator_history')
            raise Exception('404')
        c = A.AcceleratorClient('https://mempool.space/api/v1/services', get, post)
        est = c.estimate('ab' * 32)
        iid = c.create_invoice('ab' * 32, est.options[0])
        self.assertEqual(iid, 'inv123')
        inv = c.invoice(iid)
        self.assertEqual(inv.amount_sat, 77000)
        self.assertEqual(calls[1][2]['maxBidBoost'], est.options[0])
        hist_txid = fixture('accelerator_history')[0]['txid']
        self.assertIsNotNone(c.status(hist_txid))
        self.assertFalse(c.is_paid('inv123'))  # GET raises -> treated as unpaid? no: exception propagates; use None


if __name__ == '__main__':
    unittest.main()
