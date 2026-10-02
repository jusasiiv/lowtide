"""Rolling-origin backtest of the tide forecast on historical series."""
import math
import statistics
from typing import Dict, List, Optional, Sequence

from .core_forecast import fit_profile, predict, low_tide_windows, quantile, WEEK, HOUR
from .core_history import HistoryPoint


def run_backtest(hourly_points: Sequence[HistoryPoint], *, test_weeks: int = 4, train_weeks: int = 8) -> dict:
    pts = sorted(hourly_points, key=lambda p: p.ts)
    if len(pts) < 24 * 7 * 3:
        raise ValueError('not enough history')
    end = (pts[-1].ts // HOUR) * HOUR
    weeks = []
    for i in range(test_weeks):
        test_start = end - (test_weeks - i) * WEEK
        test_end = test_start + WEEK
        train = [p for p in pts if test_start - train_weeks * WEEK <= p.ts < test_start]
        test = {p.ts: p for p in pts if test_start <= p.ts < test_end}
        if len(train) < 24 * 7 * 2 or len(test) < 24 * 5:
            continue
        profile = fit_profile(train, test_start, weeks=train_weeks)
        recent = [p for p in train if test_start - 86400 <= p.ts]
        horizon, level = predict(profile, test_start, train[-1].rate, recent)
        windows, threshold = low_tide_windows(horizon)
        actual_rates = sorted(p.rate for p in test.values())
        week_median = quantile(actual_rates, 0.5)
        week_p25 = quantile(actual_rates, 0.25)
        lt_hours = [h for w in windows for h in range(w.start, w.end, HOUR) if h in test]
        hits_med = sum(1 for h in lt_hours if test[h].rate <= week_median + 1e-9)
        hits_p25 = sum(1 for h in lt_hours if test[h].rate <= week_p25 + 1e-9)
        covered = 0
        errs = []
        n = 0
        for p in horizon:
            a = test.get(p.ts)
            if a is None:
                continue
            n += 1
            if p.lo - 1e-9 <= a.rate <= p.hi + 1e-9:
                covered += 1
            errs.append(abs(math.log2(p.median / a.rate)))
        # baseline: every hour counted as low tide
        base_med = sum(1 for p in test.values() if p.rate <= week_median + 1e-9) / float(len(test))
        weeks.append({
            'test_start': test_start,
            'low_tide_hours': len(lt_hours),
            'hit_rate_vs_median': hits_med / len(lt_hours) if lt_hours else None,
            'hit_rate_vs_p25': hits_p25 / len(lt_hours) if lt_hours else None,
            'baseline_vs_median': base_med,
            'band_coverage': covered / n if n else None,
            'mae_log2': statistics.mean(errs) if errs else None,
            'week_median': week_median,
            'week_p25': week_p25,
            'level': level,
            'windows': [(w.start, w.end, w.rate) for w in windows],
        })
    tot_lt = sum(w['low_tide_hours'] for w in weeks)
    def agg(key):
        num = sum(w[key] * w['low_tide_hours'] for w in weeks if w[key] is not None)
        return num / tot_lt if tot_lt else None
    return {
        'weeks': weeks,
        'low_tide_hours': tot_lt,
        'hit_rate_vs_median': agg('hit_rate_vs_median'),
        'hit_rate_vs_p25': agg('hit_rate_vs_p25'),
        'baseline_vs_median': statistics.mean(w['baseline_vs_median'] for w in weeks) if weeks else None,
        'band_coverage': statistics.mean(w['band_coverage'] for w in weeks if w['band_coverage'] is not None) if weeks else None,
        'mae_log2': statistics.mean(w['mae_log2'] for w in weeks if w['mae_log2'] is not None) if weeks else None,
    }


def headline(result: dict) -> str:
    hr = result.get('hit_rate_vs_median')
    if hr is None:
        return 'Backtest unavailable'
    return (f"In a backtest over the last {len(result['weeks'])} weeks, predicted low-tide hours were cheaper "
            f"than the week's median {hr * 100:.0f}% of the time ({result['low_tide_hours']} hours; "
            f"always-cheap baseline {result['baseline_vs_median'] * 100:.0f}%).")
