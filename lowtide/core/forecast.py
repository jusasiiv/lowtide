"""Hour-of-week tide model with level adjustment, uncertainty band and low-tide windows."""
import datetime
import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

from .histogram import FLOOR_RATE, quantize
from .history import HistoryPoint

WEEK = 7 * 86400
HOUR = 3600

METHOD_SENTENCE = (
    "Fees follow the week: LowTide learns what each hour of the week has cost over the last "
    "12 weeks, scales that by how today is running, and calls a stretch 'low tide' when it is "
    "clearly cheaper than the rest of the coming week."
)


def slot_of(ts: int) -> int:
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.weekday() * 24 + dt.hour


def quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError('empty')
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass
class HourPoint:
    ts: int
    median: float
    lo: float
    hi: float


@dataclass
class Window:
    start: int
    end: int      # exclusive
    rate: float   # expected next-block rate inside the window (25th pct of its hours)

    @property
    def hours(self) -> int:
        return (self.end - self.start) // HOUR


@dataclass
class Forecast:
    generated_at: int
    data_as_of: int
    now_rate: float
    horizon: List[HourPoint]
    windows: List[Window]
    weekly_median: float
    threshold: float
    now_percentile: float      # share of the past week's hours that were more expensive than now
    n_weeks: int
    level: float
    method: str = METHOD_SENTENCE

    def point_at(self, ts: int) -> Optional[HourPoint]:
        if not self.horizon:
            return None
        start = self.horizon[0].ts
        i = (ts - start) // HOUR
        if 0 <= i < len(self.horizon):
            return self.horizon[i]
        return None

    def current_window(self, now: int) -> Optional[Window]:
        for w in self.windows:
            if w.start <= now < w.end:
                return w
        return None

    def next_window(self, now: int) -> Optional[Window]:
        for w in self.windows:
            if w.end > now:
                return w
        return None

    def time_until_rate_clears(self, rate: float, now: int) -> Tuple[Optional[float], Optional[float]]:
        """Seconds until the forecast median (resp. upper band) drops to `rate` or below."""
        med = None
        hi = None
        for p in self.horizon:
            t = max(0, p.ts + HOUR - now)  # end of that hour
            if med is None and p.median <= rate + 1e-9:
                med = float(t)
            if hi is None and p.hi <= rate + 1e-9:
                hi = float(t)
            if med is not None and hi is not None:
                break
        return med, hi

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Forecast':
        d = dict(d)
        d['horizon'] = [HourPoint(**p) for p in d.get('horizon', [])]
        d['windows'] = [Window(**w) for w in d.get('windows', [])]
        return cls(**d)


Profile = Dict[int, List[float]]  # slot -> sorted log-rates


def fit_profile(hourly_points: Sequence[HistoryPoint], now_ts: int, weeks: int = 12) -> Profile:
    since = now_ts - weeks * WEEK
    prof = {}  # type: Dict[int, List[float]]
    for p in hourly_points:
        if p.ts < since or p.ts > now_ts:
            continue
        prof.setdefault(slot_of(p.ts), []).append(math.log(max(FLOOR_RATE, p.rate)))
    for k in prof:
        prof[k].sort()
    return prof


def _slot_samples(profile: Profile, slot: int, all_samples: List[float]) -> List[float]:
    s = profile.get(slot)
    if s and len(s) >= 3:
        return s
    # thin slot: borrow the neighbours
    merged = list(s or [])
    for d in (-1, 1, -2, 2):
        merged += profile.get((slot + d) % 168, [])
    merged.sort()
    return merged if len(merged) >= 3 else all_samples


def level_adjustment(profile: Profile, recent: Sequence[HistoryPoint], now_ts: int,
                     all_samples: List[float]) -> float:
    """How today runs versus the profile: geometric mean ratio over the last 24 h, clamped."""
    ratios = []
    for p in recent:
        if now_ts - 86400 <= p.ts <= now_ts:
            s = _slot_samples(profile, slot_of(p.ts), all_samples)
            m = math.exp(quantile(s, 0.5))
            ratios.append(math.log(max(FLOOR_RATE, p.rate) / m))
    if not ratios:
        return 1.0
    lvl = math.exp(statistics.mean(ratios))
    return min(2.5, max(0.4, lvl))


def predict(profile: Profile, now_ts: int, live_rate: Optional[float], recent: Sequence[HistoryPoint],
            hours: int = 168, level_halflife_h: float = 48.0, blend_hours: int = 6) -> Tuple[List[HourPoint], float]:
    all_samples = sorted(x for s in profile.values() for x in s)
    if not all_samples:
        raise ValueError('empty profile')
    level = level_adjustment(profile, recent, now_ts, all_samples)
    start = (now_ts // HOUR) * HOUR
    out = []
    for h in range(hours):
        ts = start + h * HOUR
        s = _slot_samples(profile, slot_of(ts), all_samples)
        med, lo, hi = (math.exp(quantile(s, q)) for q in (0.5, 0.2, 0.8))
        f = level ** math.exp(-h / level_halflife_h)
        med, lo, hi = med * f, lo * f, hi * f
        if live_rate and h <= blend_hours:
            w = 1.0 - h / float(blend_hours)
            blended = math.exp(w * math.log(max(FLOOR_RATE, live_rate)) + (1 - w) * math.log(med))
            ratio = blended / med
            med, lo, hi = blended, lo * ratio, hi * ratio
        out.append(HourPoint(ts=ts, median=max(FLOOR_RATE, quantize(med)),
                             lo=max(FLOOR_RATE, quantize(lo)), hi=max(FLOOR_RATE, quantize(hi))))
    return out, level


def low_tide_windows(horizon: Sequence[HourPoint], *, min_hours: int = 2, merge_gap_hours: int = 1) -> Tuple[List[Window], float]:
    if not horizon:
        return [], FLOOR_RATE
    meds = sorted(p.median for p in horizon)
    threshold = max(1.5 * meds[0], quantile(meds, 0.25))
    flags = [p.median <= threshold + 1e-9 for p in horizon]
    # merge short gaps
    i = 0
    while i < len(flags):
        if not flags[i]:
            j = i
            while j < len(flags) and not flags[j]:
                j += 1
            if 0 < i and j < len(flags) and (j - i) <= merge_gap_hours:
                for k in range(i, j):
                    flags[k] = True
            i = j
        else:
            i += 1
    windows = []
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j < len(flags) and flags[j]:
                j += 1
            if j - i >= min_hours:
                rates = sorted(p.median for p in horizon[i:j])
                windows.append(Window(start=horizon[i].ts, end=horizon[j - 1].ts + HOUR, rate=quantile(rates, 0.25)))
            i = j
        else:
            i += 1
    return windows, threshold


def percentile_now(past_week: Sequence[HistoryPoint], live_rate: float) -> float:
    rates = [p.rate for p in past_week]
    if not rates:
        return 0.5
    return sum(1 for r in rates if r > live_rate + 1e-9) / float(len(rates))


def build_forecast(hourly_points: Sequence[HistoryPoint], now_ts: int, live_rate: Optional[float],
                   data_as_of: int, weeks: int = 12) -> Forecast:
    profile = fit_profile(hourly_points, now_ts, weeks)
    recent = [p for p in hourly_points if now_ts - 86400 <= p.ts <= now_ts]
    horizon, level = predict(profile, now_ts, live_rate, recent)
    windows, threshold = low_tide_windows(horizon)
    meds = sorted(p.median for p in horizon)
    past_week = [p for p in hourly_points if now_ts - WEEK <= p.ts <= now_ts]
    now_rate = live_rate if live_rate else (recent[-1].rate if recent else horizon[0].median)
    covered_weeks = 0
    if hourly_points:
        covered_weeks = int(min(weeks, (now_ts - hourly_points[0].ts) // WEEK))
    return Forecast(
        generated_at=now_ts, data_as_of=data_as_of, now_rate=now_rate, horizon=horizon, windows=windows,
        weekly_median=quantile(meds, 0.5), threshold=threshold,
        now_percentile=percentile_now(past_week, now_rate), n_weeks=covered_weeks, level=level,
    )
