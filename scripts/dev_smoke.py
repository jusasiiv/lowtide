#!/usr/bin/env python3
"""Headless smoke test of the wallet integration on the fake wallets (no GUI)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, os.path.join(HERE, '..'))
from electrum import util  # noqa: E402

loop, stop, th = util.create_and_start_event_loop()
from electrum.simple_config import SimpleConfig  # noqa: E402
from electrum.wallet import Wallet  # noqa: E402
from electrum.storage import WalletStorage  # noqa: E402
from electrum.wallet_db import WalletDB  # noqa: E402
from electrum.fee_policy import FeePolicy  # noqa: E402
from electrum.transaction import PartialTxOutput  # noqa: E402
from lowtide.qt_wallet import wallet_coins, wallet_script_info  # noqa: E402
from lowtide.core import stress as S, consolidate as C  # noqa: E402

FD = sys.argv[2]
config = SimpleConfig({'electrum_path': FD})
try:
    for name in ('lowtide_fake_segwit', 'lowtide_fake_legacy'):
        storage = WalletStorage(os.path.join(FD, 'wallets', name))
        db = WalletDB(storage.read(), storage=storage, upgrade=True)
        wallet = Wallet(db, config=config)
        coins, by_op = wallet_coins(wallet)
        st, m, n = wallet_script_info(wallet)
        tc = S.tech_check(st, m, n)
        print(f'{name}: type {st}, coins {len(coins)}, confirmed {sum(c.confirmed for c in coins)}, vsize/input {coins[0].vsize:.1f}, labels {sorted({c.label for c in coins})}')
        print('  tech:', tc.label, f'{tc.saving_pct:.0f}%', tc.ok)
        rows = S.stress_table(coins, S.default_scenarios(4.1, 0.2, {2024: 1190.0}))
        for r in rows:
            print(f'  {r.scenario.name}: {r.vsize:.0f} vB, fee {r.fee:,}, share {r.fee_share*100:.1f}%, uneconomical {len(r.uneconomical)}')
        groups = C.group_coins(coins)
        plans = C.make_plan(groups, 0.2, merge=False)
        print('  groups:', [(g.title, len(g.coins)) for g in groups], 'plans:', [(len(p.coins), p.fee) for p in plans])
        p = plans[0]
        inputs = [by_op[c.outpoint] for c in p.coins]
        addr = wallet.get_unused_address()
        tx = wallet.make_unsigned_transaction(coins=inputs, outputs=[PartialTxOutput.from_address_and_value(addr, '!')],
                                              fee_policy=FeePolicy('feerate:200'), rbf=True)
        print(f'  built tx: {len(tx.inputs())} in / {len(tx.outputs())} out, size {tx.estimated_size()} vB, fee {tx.get_fee()} sats, '
              f'rate {tx.get_fee()/tx.estimated_size():.2f} sat/vB, complete={tx.is_complete()}')
        wallet.sign_transaction(tx, None)
        print(f'  signed: complete={tx.is_complete()}, txid {tx.txid()[:16]}…')
finally:
    loop.call_soon_threadsafe(stop.set_result, 1)
    th.join(timeout=5)
