"""Wallet stress test and tech check: what every coin costs to spend at different fee levels."""
import math
from typing import Dict, List, NamedTuple, Optional, Sequence

# Approximate input vsizes per script type (single-sig), in vB.
INPUT_VSIZE = {
    'p2pkh': 148.0,
    'p2wpkh-p2sh': 91.0,
    'p2wpkh': 68.0,
    'p2tr': 57.5,
}
OUTPUT_VSIZE = {'p2pkh': 34.0, 'p2sh': 32.0, 'p2wpkh': 31.0, 'p2wsh': 43.0, 'p2tr': 43.0}
TX_OVERHEAD_VB = 10.5  # version, locktime, counts, segwit marker

TYPE_LABELS = {
    'p2pkh': 'Legacy (p2pkh)',
    'p2wpkh-p2sh': 'Wrapped segwit (p2wpkh-p2sh)',
    'p2wpkh': 'Native segwit (p2wpkh)',
    'p2sh': 'Legacy multisig (p2sh)',
    'p2wsh-p2sh': 'Wrapped segwit multisig (p2wsh-p2sh)',
    'p2wsh': 'Native segwit multisig (p2wsh)',
    'p2tr': 'Taproot (p2tr)',
}


def multisig_input_vsize(script_type: str, m: int, n: int) -> float:
    """Rough m-of-n input sizes: sigs are ~73 bytes, pubkeys 33, witness bytes weigh 1/4."""
    redeem = 3 + 34 * n  # OP_m, n pubkeys with push, OP_n, OP_CHECKMULTISIG
    sigs = 1 + 73 * m    # OP_0 + m sigs
    base = 32 + 4 + 4 + 1  # outpoint, sequence, scriptlen
    if script_type == 'p2sh':
        return base + sigs + redeem + 2
    if script_type == 'p2wsh':
        return base + (sigs + redeem + 2) / 4.0
    if script_type == 'p2wsh-p2sh':
        return base + 35 + (sigs + redeem + 2) / 4.0
    return base + sigs + redeem


def input_vsize(script_type: str, m: Optional[int] = None, n: Optional[int] = None) -> float:
    if script_type in INPUT_VSIZE:
        return INPUT_VSIZE[script_type]
    if script_type in ('p2sh', 'p2wsh', 'p2wsh-p2sh') and m and n:
        return multisig_input_vsize(script_type, m, n)
    return 148.0


class TechCheck(NamedTuple):
    script_type: str
    label: str
    input_vb: float
    best_type: str
    best_vb: float
    saving_pct: float
    ok: bool
    message: str


def tech_check(script_type: str, m: Optional[int] = None, n: Optional[int] = None) -> TechCheck:
    """Compare this wallet's input cost with the cheapest type Electrum can create."""
    multisig = script_type in ('p2sh', 'p2wsh', 'p2wsh-p2sh')
    cur = input_vsize(script_type, m, n)
    if multisig:
        best_type, best = 'p2wsh', multisig_input_vsize('p2wsh', m or 2, n or 3)
    else:
        best_type, best = 'p2wpkh', INPUT_VSIZE['p2wpkh']
    saving = max(0.0, 1.0 - best / cur) * 100 if cur else 0.0
    label = TYPE_LABELS.get(script_type, script_type)
    if script_type in ('p2wpkh', 'p2wsh', 'p2tr'):
        msg = '✓ ' + ('The cheapest wallet type Electrum offers.' if script_type != 'p2wsh' else 'Native segwit multisig: the cheapest multisig type Electrum offers.')
        return TechCheck(script_type, label, cur, best_type, best, 0.0, True, msg)
    if multisig:
        msg = (f'Each input costs ~{cur:.0f} vB; native segwit multisig (p2wsh) would cost ~{best:.0f} vB, '
               f'{saving:.0f}% less. Create a new p2wsh multisig wallet with the same cosigners and migrate at low tide.')
    elif script_type == 'p2pkh':
        msg = (f'Each input costs ~{cur:.0f} vB; native segwit costs ~{best:.0f} vB, {saving:.0f}% less. '
               f'Create a new native segwit wallet and migrate at low tide.')
    else:
        msg = (f'Each input costs ~{cur:.0f} vB; native segwit costs ~{best:.0f} vB, {saving:.0f}% less. '
               f'Recommend native segwit (p2wpkh) for new wallets; migrate at low tide.')
    return TechCheck(script_type, label, cur, best_type, best, saving, False, msg)


class Coin(NamedTuple):
    outpoint: str
    value: int
    vsize: float          # input vsize for this coin in this wallet
    label: str            # tx label, else address label, else ''
    txid: str
    confirmed: bool
    frozen: bool
    address: str = ''


class Scenario(NamedTuple):
    name: str
    rate: float           # sat/vB
    kind: str             # 'now' | 'lowtide' | 'spike' | 'custom'


class StressRow(NamedTuple):
    scenario: Scenario
    n_coins: int
    vsize: float
    fee: int
    balance: int
    fee_share: float      # fee / balance
    uneconomical: List[Coin]
    uneconomical_value: int


def spend_all_vsize(coins: Sequence[Coin], *, n_outputs: int = 1, output_type: str = 'p2wpkh') -> float:
    return TX_OVERHEAD_VB + sum(c.vsize for c in coins) + n_outputs * OUTPUT_VSIZE.get(output_type, 31.0)


def fee_for(vsize: float, rate: float) -> int:
    return int(math.ceil(vsize * rate))


def stress_row(coins: Sequence[Coin], scenario: Scenario, *, output_type: str = 'p2wpkh') -> StressRow:
    vsize = spend_all_vsize(coins, output_type=output_type)
    fee = fee_for(vsize, scenario.rate)
    balance = sum(c.value for c in coins)
    unecon = [c for c in coins if fee_for(c.vsize, scenario.rate) > c.value]
    return StressRow(scenario, len(coins), vsize, fee, balance, (fee / balance) if balance else 0.0,
                     unecon, sum(c.value for c in unecon))


def stress_table(coins: Sequence[Coin], scenarios: Sequence[Scenario], *, output_type: str = 'p2wpkh') -> List[StressRow]:
    return [stress_row(coins, s, output_type=output_type) for s in scenarios]


def default_scenarios(now_rate: Optional[float], low_tide_rate: Optional[float], spikes: Dict[int, float],
                      custom_rate: Optional[float] = None) -> List[Scenario]:
    out = []
    if now_rate is not None:
        out.append(Scenario('Now (next block)', now_rate, 'now'))
    if low_tide_rate is not None:
        out.append(Scenario('Next low tide', low_tide_rate, 'lowtide'))
    for year in sorted(spikes):
        out.append(Scenario(f'{year} spike', spikes[year], 'spike'))
    if custom_rate is not None:
        out.append(Scenario('Custom', custom_rate, 'custom'))
    return out
