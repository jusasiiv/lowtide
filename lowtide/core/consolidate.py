"""Privacy-aware consolidation planning."""
import math
from typing import Dict, List, NamedTuple, Optional, Sequence

from .stress import Coin, TX_OVERHEAD_VB, OUTPUT_VSIZE, fee_for

UNLABELED = ''


class Group(NamedTuple):
    key: str              # label, or '' for unlabeled
    coins: List[Coin]
    value: int
    sources: int          # distinct funding transactions

    @property
    def title(self) -> str:
        return self.key or 'Unlabeled'


def group_coins(coins: Sequence[Coin], *, skip_frozen: bool = True, skip_unconfirmed: bool = True) -> List[Group]:
    """Group by label. Coins with different labels are never merged by default."""
    buckets = {}  # type: Dict[str, List[Coin]]
    for c in coins:
        if skip_frozen and c.frozen:
            continue
        if skip_unconfirmed and not c.confirmed:
            continue
        buckets.setdefault(c.label.strip(), []).append(c)
    groups = []
    for key, cs in buckets.items():
        cs = sorted(cs, key=lambda c: c.value)
        groups.append(Group(key, cs, sum(c.value for c in cs), len({c.txid for c in cs})))
    groups.sort(key=lambda g: (g.key == UNLABELED, -len(g.coins)))
    return groups


class Plan(NamedTuple):
    coins: List[Coin]
    groups: List[Group]
    vsize: float
    fee: int
    value: int
    output_value: int
    rate: float
    links_groups: bool
    uneconomical: List[Coin]


def make_plan(groups: Sequence[Group], rate: float, *, merge: bool, output_type: str = 'p2wpkh') -> List[Plan]:
    """One plan per group, or a single merged plan (which links the groups) when `merge`."""
    def plan_for(gs: Sequence[Group]) -> Optional[Plan]:
        coins = [c for g in gs for c in g.coins]
        if len(coins) < 2:
            return None
        vsize = TX_OVERHEAD_VB + sum(c.vsize for c in coins) + OUTPUT_VSIZE.get(output_type, 31.0)
        fee = fee_for(vsize, rate)
        value = sum(c.value for c in coins)
        if value - fee <= 0:
            return None
        unecon = [c for c in coins if fee_for(c.vsize, rate) > c.value]
        return Plan(coins, list(gs), vsize, fee, value, value - fee, rate, len(gs) > 1, unecon)
    if merge:
        p = plan_for(groups)
        return [p] if p else []
    out = []
    for g in groups:
        p = plan_for([g])
        if p:
            out.append(p)
    return out


def contrast(vsize: float, low_rate: float, spike_rate: float) -> str:
    return (f"{vsize:.0f} vB: {fee_for(vsize, low_rate):,} sats at {low_rate:g} sat/vB now, "
            f"versus {fee_for(vsize, spike_rate):,} sats at a {spike_rate:g} sat/vB spike.")


def linking_warning(groups: Sequence[Group]) -> str:
    if len(groups) <= 1:
        g = groups[0] if groups else None
        if g and g.sources > 1:
            return f"This links {len(g.coins)} coins from {g.sources} transactions, all labeled '{g.title}'."
        return ''
    names = ', '.join(f"'{g.title}'" for g in groups)
    return f"This will publicly link {names} as one owner. Only do this if that is acceptable."
