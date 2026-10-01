"""Turn mempool.space statistics into a history of the 'next-block rate'."""
import math
import statistics
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from .histogram import Hist, FLOOR_RATE, BLOCK_VB, quantize
from .mempool_api import STATISTICS_BANDS

INF = math.inf


def band_edges() -> List[tuple]:
    return [(lo, (STATISTICS_BANDS[i + 1] if i + 1 < len(STATISTICS_BANDS) else INF))
            for i, lo in enumerate(STATISTICS_BANDS)]


EDGES = band_edges()


class HistoryPoint(NamedTuple):
    ts: int
    rate: float        # next-block rate estimate, sat/vB
    sub1: bool         # True when the cut-off fell inside the [0,1) band
    above1_vb: float
    above2_vb: float
    sub1_vb: float
    total_vb: float
    vps: float
    min_fee: float


Sub1Resolver = Callable[[float], float]


def sub1_resolver_from_hist(live_hist: Hist) -> Sub1Resolver:
    """Resolve rates inside the [0,1) band using the live fine histogram's sub-1 shape.

    The floor pile is persistent, so its shape today is a fair prior for its shape last week.
    `need_vb` is how much of the sub-1 mass must fit into the next block; the returned rate is
    the fee at which the live sub-1 cumulative (from 1.0 downwards) reaches that amount.
    """
    sub1 = [(f, v) for f, v in live_hist if f < 1.0]  # already sorted descending
    def resolve(need_vb: float) -> float:
        cum = 0.0
        for fee, vsize in sub1:
            cum += vsize
            if cum >= need_vb:
                return max(FLOOR_RATE, quantize(math.ceil(fee * 10 - 1e-9) / 10))
        return FLOOR_RATE
    return resolve


def rate_from_bands(vsizes: Sequence[float], depth_vb: float = BLOCK_VB,
                    sub1: Optional[Sub1Resolver] = None) -> tuple:
    """(rate, in_band0) needed to be within depth_vb, from the 39 statistics bands."""
    cum = 0.0
    for i in range(len(vsizes) - 1, -1, -1):
        v = vsizes[i]
        lo, hi = EDGES[i]
        if v <= 0:
            continue
        if cum + v >= depth_vb:
            need = depth_vb - cum
            if i == 0:
                rate = sub1(need) if sub1 else 0.5
                return max(FLOOR_RATE, rate), True
            if hi is INF:
                return float(lo), False
            frac = min(1.0, max(0.0, need / v))
            # a sliver of the band -> near its top; the whole band -> its bottom
            return max(FLOOR_RATE, hi - (hi - lo) * frac), False
        cum += v
    return FLOOR_RATE, True


def to_point(p: dict, sub1: Optional[Sub1Resolver] = None) -> HistoryPoint:
    v = p['vsizes']
    rate, in0 = rate_from_bands(v, sub1=sub1)
    return HistoryPoint(
        ts=p['added'], rate=rate, sub1=in0,
        above1_vb=sum(v[1:]), above2_vb=sum(v[2:]), sub1_vb=v[0], total_vb=sum(v),
        vps=p.get('vps', 0.0), min_fee=p.get('min_fee', 0.0),
    )


def merge_series(*series: Sequence[dict], step: int = 1800, sub1: Optional[Sub1Resolver] = None) -> List[HistoryPoint]:
    """Merge statistics series (coarsest first, finest last) into one series with one point per `step`.

    Finer series override coarser ones in the buckets they cover.
    """
    buckets = {}  # type: Dict[int, dict]
    for s in series:
        for p in s:
            buckets[p['added'] // step] = p
    return [to_point(buckets[k], sub1) for k in sorted(buckets)]


def hourly(series: Sequence[HistoryPoint]) -> List[HistoryPoint]:
    """One point per hour: median rate of the points in that hour, sums/means for the rest."""
    groups = {}  # type: Dict[int, List[HistoryPoint]]
    for p in series:
        groups.setdefault(p.ts // 3600, []).append(p)
    out = []
    for h in sorted(groups):
        pts = groups[h]
        out.append(HistoryPoint(
            ts=h * 3600,
            rate=statistics.median(p.rate for p in pts),
            sub1=all(p.sub1 for p in pts),
            above1_vb=statistics.mean(p.above1_vb for p in pts),
            above2_vb=statistics.mean(p.above2_vb for p in pts),
            sub1_vb=statistics.mean(p.sub1_vb for p in pts),
            total_vb=statistics.mean(p.total_vb for p in pts),
            vps=statistics.mean(p.vps for p in pts),
            min_fee=min(p.min_fee for p in pts),
        ))
    return out
