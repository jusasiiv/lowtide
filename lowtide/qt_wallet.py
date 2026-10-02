"""Wallet → core adapters (Electrum types in, plain core types out)."""
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from electrum.transaction import Transaction, PartialTxInput

from .core_stress import Coin, input_vsize

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet


def wallet_script_info(wallet: 'Abstract_Wallet') -> Tuple[str, Optional[int], Optional[int]]:
    """(script_type, m, n) for the wallet; m/n only for multisig."""
    script_type = getattr(wallet, 'txin_type', None) or 'p2pkh'
    m = getattr(wallet, 'm', None)
    n = getattr(wallet, 'n', None)
    return script_type, m, n


def is_segwit_type(script_type: str) -> bool:
    return script_type in ('p2wpkh', 'p2wpkh-p2sh', 'p2wsh', 'p2wsh-p2sh', 'p2tr')


def wallet_coins(wallet: 'Abstract_Wallet') -> Tuple[List[Coin], Dict[str, PartialTxInput]]:
    """All of the wallet's coins as core Coins, plus a map outpoint -> PartialTxInput for tx building."""
    script_type, m, n = wallet_script_info(wallet)
    fallback_vb = input_vsize(script_type, m, n)
    segwit = is_segwit_type(script_type)
    coins = []
    by_outpoint = {}
    for u in wallet.get_utxos():
        try:
            wallet.add_input_info(u)
            vsize = Transaction.estimated_input_weight(u, segwit) / 4.0
        except Exception:
            vsize = fallback_vb
        txid = u.prevout.txid.hex()
        outpoint = u.prevout.to_str()
        try:
            height = wallet.adb.get_tx_height(txid).height()
        except Exception:
            height = 0
        label = wallet.get_label_for_txid(txid) or wallet.get_label_for_address(u.address) or ''
        frozen = bool(wallet.is_frozen_coin(u) or wallet.is_frozen_address(u.address))
        coins.append(Coin(outpoint=outpoint, value=int(u.value_sats() or 0), vsize=vsize, label=label, txid=txid,
                          confirmed=height > 0, frozen=frozen, address=u.address or ''))
        by_outpoint[outpoint] = u
    return coins, by_outpoint
