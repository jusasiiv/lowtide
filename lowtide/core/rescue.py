"""Stuck transaction rescue: compare Wait / small RBF step / RBF / CPFP / Accelerate honestly."""
import math
from typing import Callable, List, NamedTuple, Optional

from .eta import Eta
from .histogram import FLOOR_RATE, STEP, quantize, quantize_up

CPFP_CHILD_VB = {'p2wpkh': 110.0, 'p2wpkh-p2sh': 134.0, 'p2pkh': 192.0, 'p2wsh': 150.0, 'p2sh': 340.0, 'p2wsh-p2sh': 185.0}
MEMPOOL_EXPIRY_S = 14 * 86400


class TxFacts(NamedTuple):
    txid: str
    vsize: int
    fee: int
    age_s: float
    can_bump: bool            # Electrum can RBF it (our inputs, signalling RBF)
    can_cpfp: bool            # we control an output
    script_type: str
    has_lightning_sats: int   # spendable Lightning balance, 0 if none
    incoming: bool            # someone else's payment to us
    relay_rate: float         # the server's min relay rate (sat/vB)

    @property
    def rate(self) -> float:
        if self.fee < 0 or not self.vsize:
            return 0.0
        return self.fee / self.vsize


class Option(NamedTuple):
    key: str
    title: str
    available: bool
    reason: str               # why unavailable, or a short note
    cost_sats: Optional[int]
    new_rate: Optional[float]
    eta: Optional[Eta]
    needs_signing: bool
    changes_txid: bool
    needs_spare_coins: bool
    detail: str


EtaFn = Callable[[float], Optional[Eta]]


def _bump_cost(facts: TxFacts, target: float) -> int:
    return max(0, int(math.ceil(target * facts.vsize)) - max(0, facts.fee))


def _min_bump_rate(facts: TxFacts) -> float:
    """BIP-125: the replacement pays at least old fee + relay fee for its size."""
    return quantize_up(facts.rate + max(facts.relay_rate, FLOOR_RATE))


def compare(facts: TxFacts, *, eta_fn: EtaFn, next_block_rate: Optional[float], pile_rate: Optional[float],
            deadline_rate: Optional[float], mempool_min_fee: float, accel_total: Optional[int], accel_reason: str = '',
            accel_eta: Optional[Eta] = None) -> List[Option]:
    opts = []
    # Wait
    e = eta_fn(facts.rate) if facts.fee >= 0 else None
    evicting = facts.fee >= 0 and facts.rate < mempool_min_fee - 1e-9
    expires_in = MEMPOOL_EXPIRY_S - facts.age_s
    note = ''
    if evicting:
        note = 'below the mempool minimum: nodes are dropping it'
    elif expires_in < 2 * 86400:
        note = 'close to the 2-week mempool expiry'
    opts.append(Option('wait', 'Wait', True, note, 0, facts.rate, e, False, False, False, 'Free. Confirms when the tide drops below your rate.'))

    # Small RBF step: to the pile-jump rate when stuck in the floor pile
    if pile_rate is not None and facts.rate < pile_rate - 1e-9:
        target = max(pile_rate, _min_bump_rate(facts))
        avail, reason = facts.can_bump, ''
        if not facts.can_bump:
            reason = 'not ours to replace' if facts.incoming else 'Electrum cannot replace this transaction'
        opts.append(Option('small_rbf', 'Small RBF step', avail, reason, _bump_cost(facts, target) if avail else None, target,
                           eta_fn(target), True, True, False, f'Bump to {target:g} sat/vB: ahead of the floor pile for tens of sats.'))
    else:
        opts.append(Option('small_rbf', 'Small RBF step', False, 'already above the floor pile', None, None, None, True, True, False, ''))

    # RBF to the deadline (or to the next block)
    target = deadline_rate if deadline_rate is not None else next_block_rate
    if target is not None:
        target = max(quantize(target), _min_bump_rate(facts))
        avail, reason = facts.can_bump, ''
        if not facts.can_bump:
            reason = 'not ours to replace' if facts.incoming else 'Electrum cannot replace this transaction'
        opts.append(Option('rbf', 'RBF bump', avail, reason, _bump_cost(facts, target) if avail else None, target, eta_fn(target),
                           True, True, False, f'Replace with the same payment at {target:g} sat/vB (Electrum bump-fee).'))
    else:
        opts.append(Option('rbf', 'RBF bump', False, 'no fee estimate yet', None, None, None, True, True, False, ''))

    # CPFP
    child_vb = CPFP_CHILD_VB.get(facts.script_type, 150.0)
    if target is not None:
        pkg_target = max(quantize(target), FLOOR_RATE)
        child_fee = max(0, int(math.ceil(pkg_target * (facts.vsize + child_vb))) - max(0, facts.fee))
        avail, reason = facts.can_cpfp, ''
        if not facts.can_cpfp:
            reason = 'no output of ours to spend'
        opts.append(Option('cpfp', 'CPFP', avail, reason, child_fee if avail else None, pkg_target, eta_fn(pkg_target),
                           True, False, False, f'Spend our output in a child paying {child_fee:,} sats; the pair is mined at {pkg_target:g} sat/vB.'))
    else:
        opts.append(Option('cpfp', 'CPFP', False, 'no fee estimate yet', None, None, None, True, False, False, ''))

    # Accelerate
    if accel_total is not None:
        opts.append(Option('accelerate', 'mempool Accelerator', True, '', accel_total, next_block_rate, accel_eta or (eta_fn(next_block_rate) if next_block_rate else None),
                           False, False, False, 'Paid out-of-band (Lightning or on-chain); no signing, txid unchanged, works for incoming payments.'))
    else:
        opts.append(Option('accelerate', 'mempool Accelerator', False, accel_reason or 'unavailable', None, None, None, False, False, False, ''))
    return opts


class Recommendation(NamedTuple):
    option: Optional[Option]
    why: str


def recommend(options: List[Option], deadline_s: Optional[float]) -> Recommendation:
    avail = [o for o in options if o.available]
    if not avail:
        return Recommendation(None, 'No option is available for this transaction.')

    def meets(o: Option) -> bool:
        if deadline_s is None:
            return True
        if o.eta is None or o.eta.p90_s is None:
            return False
        return o.eta.p90_s <= deadline_s

    ok = [o for o in avail if meets(o)]
    if ok:
        best = min(ok, key=lambda o: (o.cost_sats if o.cost_sats is not None else 0, o.eta.p90_s if o.eta and o.eta.p90_s else 0))
        if best.key == 'wait':
            why = 'Waiting is free and the forecast says it confirms in time.'
        elif best.key == 'small_rbf':
            why = f'A {best.cost_sats:,}-sat bump jumps the floor pile; cheaper than everything else that meets the deadline.'
        else:
            why = f'Cheapest option that confirms before the deadline ({best.cost_sats:,} sats).'
        return Recommendation(best, why)
    fastest = min(avail, key=lambda o: (o.eta.p90_s if o.eta and o.eta.p90_s is not None else float('inf')))
    return Recommendation(fastest, 'Nothing meets the deadline with 90% confidence; this is the fastest available option.')
