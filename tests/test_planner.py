import unittest

from lowtide.core import planner as P
from lowtide.core.forecast import Forecast, Window, HourPoint


def forecast(now, windows):
    return Forecast(generated_at=now, data_as_of=now, now_rate=2.0, horizon=[HourPoint(now + i * 3600, 1.0, 0.5, 2.0) for i in range(168)],
                    windows=windows, weekly_median=1.0, threshold=0.6, now_percentile=0.5, n_weeks=12, level=1.0)


class PlannerTests(unittest.TestCase):

    def setUp(self):
        self.now = 1_800_000_000 - (1_800_000_000 % 3600)
        self.w1 = Window(start=self.now + 10 * 3600, end=self.now + 16 * 3600, rate=0.5)
        self.w2 = Window(start=self.now + 40 * 3600, end=self.now + 50 * 3600, rate=0.3)
        self.fc = forecast(self.now, [self.w1, self.w2])
        self.rfd = lambda secs: 3.0 if secs < 7200 else 1.5

    def item(self, **kw):
        d = dict(id='a', kind='payment', address='bc1q', amount_sat=10000, label='x', deadline=None, max_feerate=None, status='queued')
        d.update(kw)
        return d

    def test_no_deadline_picks_cheapest_window(self):
        [it] = P.plan_items([self.item()], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0)
        self.assertEqual(it['planned_start'], self.w2.start)
        self.assertEqual(it['planned_rate'], 0.3)

    def test_deadline_restricts_windows(self):
        [it] = P.plan_items([self.item(deadline=self.now + 30 * 3600)], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0)
        self.assertEqual(it['planned_start'], self.w1.start)   # w2 ends after the deadline
        self.assertEqual(it['planned_rate'], 0.5)

    def test_tight_deadline_sends_now(self):
        [it] = P.plan_items([self.item(deadline=self.now + 4 * 3600)], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0)
        self.assertEqual(it['planned_start'], self.now)
        self.assertEqual(it['planned_rate'], 1.5)
        self.assertIn('send now', it['plan_note'])

    def test_consolidation_uses_floor_and_cap(self):
        [c] = P.plan_items([self.item(kind='consolidation')], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0)
        self.assertEqual(c['planned_rate'], 0.2)
        [p] = P.plan_items([self.item(deadline=self.now + 4 * 3600, max_feerate=1.0)], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0)
        self.assertEqual(p['planned_rate'], 1.0)

    def test_demo_low_tide_now(self):
        items = P.plan_items([self.item()], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0, demo={'low_tide_now': True})
        self.assertEqual(items[0]['planned_start'], self.now)
        self.assertEqual(len(P.due_items(items, self.now, demo={'low_tide_now': True})), 1)
        self.assertEqual(len(P.due_items(P.plan_items([self.item()], self.fc, self.now, floor_rate=0.2, rate_for_deadline=self.rfd, next_block_rate=2.0), self.now)), 0)

    def test_guard_and_overdue(self):
        sent = self.item(status='sent', txid='t1', deadline=self.now + 3600)
        self.assertEqual(len(P.guard_items([sent], self.now, {'t1'})), 1)
        self.assertEqual(len(P.guard_items([sent], self.now, set())), 0)
        late = self.item(deadline=self.now - 10)
        self.assertEqual(len(P.overdue_items([late], self.now)), 1)
        self.assertEqual(P.batch_rate([{'planned_rate': 0.3}, {'planned_rate': 1.2}]), 1.2)


if __name__ == '__main__':
    unittest.main()
