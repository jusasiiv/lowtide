"""Fine-grained mempool fee histogram math.

A histogram is a list of (feerate_sat_per_vb, vsize) pairs. Both mempool.space
(`/api/mempool` → fee_histogram) and the Electrum server protocol
(`mempool.get_fee_histogram`) use this shape, sorted by descending fee rate.
"""
import math
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

Hist = List[Tuple[float, int]]

FLOOR_RATE = 0.1          # Electrum's FEERATE_MIN_RELAY, and mempool's minimum since 2025
STEP = 0.1                # Electrum's fee rate precision
BLOCK_VB = 1_000_000      # one block of vsize


def quantize_up(rate: float, step: float = STEP) -> float:
    """Round a rate up to the next 0.1 sat/vB (what you must pay to be at least `rate`)."""
    return round(math.ceil(round(rate / step, 6)) * step, 6)


def quantize(rate: float, step: float = STEP) -> float:
    return round(math.floor(rate / step + 0.5 + 1e-9) * step, 6)


def normalize(hist: Iterable[Sequence]) -> Hist:
    """Sort descending by fee rate, drop empty and malformed entries."""
    out = []
    for item in hist:
        try:
            fee, vsize = float(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if vsize <= 0:
            continue
        out.append((fee, vsize))
    out.sort(key=lambda x: -x[0])
    return out


def total_vsize(hist: Hist) -> int:
    return sum(v for _, v in hist)


def vsize_at_or_above(hist: Hist, rate: float) -> int:
    """vsize that would be ahead of a transaction paying exactly `rate` (ties are pessimistic)."""
    return sum(v for f, v in hist if f >= rate - 1e-9)


def vsize_below(hist: Hist, rate: float) -> int:
    """vsize that a transaction paying `rate` jumps ahead of."""
    return sum(v for f, v in hist if f < rate - 1e-9)


def min_fee(hist: Hist) -> float:
    """Lowest fee rate with mass in the mempool (zero-fee entries ignored), at least the floor."""
    fees = [f for f, _ in hist if f > 0]
    if not fees:
        return FLOOR_RATE
    return max(FLOOR_RATE, min(fees))


def rate_at_depth(hist: Hist, depth_vb: float) -> float:
    """Fee rate needed to be within the first `depth_vb` of the mempool (e.g. 1 vMB = next block).

    Returns a rate quantized up to 0.1 sat/vB, never below the floor. If the whole
    mempool is smaller than depth_vb, the floor is enough.
    """
    cum = 0
    for fee, vsize in hist:
        cum += vsize
        if cum >= depth_vb:
            return max(FLOOR_RATE, quantize_up(fee))
    return FLOOR_RATE


def next_block_rate(hist: Hist) -> float:
    return rate_at_depth(hist, BLOCK_VB)


def bins(hist: Hist, step: float = STEP, fine_until: float = 2.0,
         coarse_edges: Sequence[float] = (3, 4, 5, 6, 8, 10, 15, 20, 30, 50, 100)) -> List[Tuple[float, float, int]]:
    """Bucket the histogram: 0.1 steps up to `fine_until`, then coarse edges, then one open bucket.

    Returns a list of (lower, upper, vsize), ascending; `upper` is math.inf for the last bucket.
    """
    edges = [round(i * step, 6) for i in range(int(round(fine_until / step)) + 1)]
    for e in coarse_edges:
        if e > fine_until:
            edges.append(float(e))
    edges.append(math.inf)
    out = []
    for lo, hi in zip(edges, edges[1:]):
        v = sum(vs for f, vs in hist if lo - 1e-9 <= f < hi - 1e-9)
        out.append((lo, hi, v))
    return out


class PileJump(NamedTuple):
    rate: float            # the pile-jump rate, 0.1 steps
    ahead_vb: int          # vsize below rate (the pile you jump)
    behind_vb: int         # vsize at or above rate (what is still ahead of you)
    ceiling_rate: float    # the rate we compared against (1.0 sat/vB by default)
    ceiling_behind_vb: int # what is ahead of you when paying the ceiling rate
    pile_min_fee: float    # where the pile starts


def pile_jump(hist: Hist, *, ceiling: float = 1.0, within_vb: int = 500_000,
              min_pile_vb: int = 1_000_000) -> Optional[PileJump]:
    """Cheapest 0.1-step rate that lands within `within_vb` of paying `ceiling`.

    This is the "jump the floor pile" rate: nearly the same queue position as the
    ceiling for a fraction of the cost. Returns None when there is no meaningful pile.
    """
    pile_vb = vsize_below(hist, ceiling)
    if pile_vb < min_pile_vb:
        return None
    base_behind = vsize_at_or_above(hist, ceiling)
    start = max(FLOOR_RATE, quantize_up(min_fee(hist) + STEP))
    x = start
    while x < ceiling - 1e-9:
        behind = vsize_at_or_above(hist, x)
        if behind - base_behind <= within_vb:
            return PileJump(rate=quantize(x), ahead_vb=vsize_below(hist, x), behind_vb=behind,
                            ceiling_rate=ceiling, ceiling_behind_vb=base_behind, pile_min_fee=min_fee(hist))
        x = quantize(x + STEP)
    return PileJump(rate=quantize(ceiling), ahead_vb=pile_vb, behind_vb=base_behind,
                    ceiling_rate=ceiling, ceiling_behind_vb=base_behind, pile_min_fee=min_fee(hist))


def sub1_vsize(hist: Hist) -> int:
    return vsize_below(hist, 1.0)
