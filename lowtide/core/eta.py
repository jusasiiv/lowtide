"""Confirmation time model: queue ahead + arrivals above the rate + Poisson blocks."""
import math
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

from .histogram import Hist, vsize_at_or_above, quantize, quantize_up, FLOOR_RATE, STEP, BLOCK_VB

BLOCK_INTERVAL = 600.0  # seconds


def poisson_cdf(k_minus_1: int, lam: float) -> float:
    """P(N <= k-1) for N ~ Poisson(lam), computed in log space."""
    if lam <= 0:
        return 1.0
    total = 0.0
    log_lam = math.log(lam)
    for i in range(k_minus_1 + 1):
        total += math.exp(-lam + i * log_lam - math.lgamma(i + 1))
    return min(1.0, total)


def erlang_quantile(k: int, mean_interval: float, q: float) -> float:
    """Time by which at least k Poisson events have happened with probability q."""
    k = max(1, int(k))
    lo, hi = 0.0, max(mean_interval * k * 8.0, mean_interval)
    # P(N(t) >= k) = 1 - poisson_cdf(k-1, t/mean)
    for _ in range(60):
        mid = (lo + hi) / 2
        p = 1.0 - poisson_cdf(k - 1, mid / mean_interval)
        if p < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def blocks_needed(queue_vb: float, arrival_vb_per_s: float, *, block_vb: float = BLOCK_VB,
                  block_interval: float = BLOCK_INTERVAL) -> Optional[int]:
    """Blocks until a queue of `queue_vb` drains while `arrival_vb_per_s` keeps arriving above you.

    None means the queue never drains at this rate (arrivals exceed capacity).
    """
    net_per_block = block_vb - arrival_vb_per_s * block_interval
    if net_per_block <= 0:
        return None
    return max(1, math.ceil(queue_vb / net_per_block + 1e-9))


class Eta(NamedTuple):
    rate: float
    queue_vb: int
    arrival_vb_per_s: float
    blocks: Optional[int]      # None: does not drain until the tide turns
    median_s: Optional[float]  # seconds from now
    p90_s: Optional[float]
    note: str


ArrivalFn = Callable[[float], float]


TIDE_DEFER_S = 2 * 3600.0  # beyond this, trust the tide forecast over the steady-state drain model

TideFn = Callable[[float], Tuple[Optional[float], Optional[float]]]


def eta_for_rate(hist: Hist, rate: float, arrival_fn: ArrivalFn, *, tide_fn: Optional[TideFn] = None) -> Eta:
    """ETA for a transaction paying `rate` sat/vB given the live histogram.

    `arrival_fn(rate)` returns the vB/s arriving at or above `rate`.
    `tide_fn(rate)` returns (median_s, p90_s) until the forecast says `rate` clears; used when the
    queue above `rate` does not drain soon in steady state (the sub-1 and near-1 cases).
    """
    rate = max(FLOOR_RATE, quantize(rate))
    queue = vsize_at_or_above(hist, rate)
    arrival = max(0.0, float(arrival_fn(rate)))
    k = blocks_needed(queue, arrival)
    med = p90 = None
    if k is not None:
        med = erlang_quantile(k, BLOCK_INTERVAL, 0.5)
        p90 = erlang_quantile(k, BLOCK_INTERVAL, 0.9)
    if (k is None or med > TIDE_DEFER_S) and tide_fn is not None:
        t_med, t_hi = tide_fn(rate)
        if t_med is not None:
            t_med += BLOCK_INTERVAL
            t_hi = (t_hi + 3 * BLOCK_INTERVAL) if t_hi is not None else None
            if med is None or t_med < med:
                med, p90 = t_med, (t_hi if t_hi is not None else (p90 if p90 is not None else None))
                return Eta(rate, queue, arrival, k, med, p90, 'when the tide turns')
    if k is None:
        return Eta(rate, queue, arrival, None, None, None, 'not before the tide turns')
    return Eta(rate, queue, arrival, k, med, p90, '')


def candidate_rates(hist: Hist, *, fine_until: float = 2.0) -> List[float]:
    """Rates worth testing: every 0.1 step up to `fine_until`, then each histogram level rounded up."""
    cands = {quantize(FLOOR_RATE + i * STEP) for i in range(int(round((fine_until - FLOOR_RATE) / STEP)) + 1)}
    for fee, _ in hist:
        if fee > fine_until:
            cands.add(quantize_up(fee))
    return sorted(cands)


def rate_for_deadline(hist: Hist, deadline_s: float, arrival_fn: ArrivalFn, *,
                      confidence: float = 0.9, max_rate: Optional[float] = None,
                      tide_fn: Optional[TideFn] = None) -> Optional[Eta]:
    """Cheapest rate whose `confidence` quantile ETA fits within `deadline_s` seconds."""
    for x in candidate_rates(hist):
        if max_rate is not None and x > max_rate + 1e-9:
            break
        e = eta_for_rate(hist, x, arrival_fn, tide_fn=tide_fn)
        bound = e.p90_s if confidence >= 0.9 else e.median_s
        if bound is not None and bound <= deadline_s:
            return e
    return None


# --- arrival-rate estimation from the newest mined blocks -----------------------

PERCENTILE_POINTS = (0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 1.0)


def fraction_above(fee_range: Sequence[float], rate: float) -> float:
    """Share of a block's vsize paying at least `rate`, from mempool's 7-point feeRange."""
    if not fee_range or len(fee_range) != len(PERCENTILE_POINTS):
        return 0.0
    fr = list(fee_range)
    if rate <= fr[0]:
        return 1.0
    if rate >= fr[-1]:
        return 0.0
    for (p0, f0), (p1, f1) in zip(zip(PERCENTILE_POINTS, fr), zip(PERCENTILE_POINTS[1:], fr[1:])):
        if f0 <= rate <= f1:
            if f1 == f0:
                return 1.0 - p1
            frac = (rate - f0) / (f1 - f0)
            return 1.0 - (p0 + (p1 - p0) * frac)
    return 0.0


def arrival_rate_from_blocks(blocks: Sequence[dict], rate: float) -> float:
    """vB/s mined at or above `rate` over the span of the given blocks.

    Steady-state heuristic: if the queue above `rate` is roughly stable, what gets
    mined above it per second is what arrives above it per second.
    """
    if not blocks:
        return 0.0
    mined = 0.0
    ts = []
    for b in blocks:
        extras = b.get('extras') or {}
        fr = extras.get('feeRange') or []
        vsize = (b.get('weight') or 0) / 4.0
        mined += vsize * fraction_above(fr, rate)
        if b.get('timestamp'):
            ts.append(b['timestamp'])
    if len(ts) < 2:
        span = BLOCK_INTERVAL * len(blocks)
    else:
        span = (max(ts) - min(ts)) + BLOCK_INTERVAL  # include the newest block's own interval
    span = max(span, BLOCK_INTERVAL)
    return mined / span


def make_arrival_fn(blocks: Sequence[dict], snapshot_delta: Optional[Tuple[Hist, Hist, float]] = None) -> ArrivalFn:
    """Arrival estimator: mined-above rate, plus the observed queue growth between two snapshots."""
    def fn(rate: float) -> float:
        base = arrival_rate_from_blocks(blocks, rate)
        if snapshot_delta:
            older, newer, dt = snapshot_delta
            if dt > 0:
                growth = (vsize_at_or_above(newer, rate) - vsize_at_or_above(older, rate)) / dt
                base = max(0.0, base + growth)
        return base
    return fn
