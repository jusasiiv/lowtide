#!/usr/bin/env python3
"""Create mainnet wallets with FAKE confirmed coins for offline GUI testing.

Usage: dev_fake_wallets.py <electrum_src_dir> <datadir>
Run the GUI with: run_electrum --offline -D <datadir> -w <datadir>/wallets/lowtide_fake_segwit
The coins do not exist on the network; never connect this data dir to a server expecting them to stay.
"""
import os
import random
import struct
import sys
import time

sys.path.insert(0, sys.argv[1])
from electrum.simple_config import SimpleConfig  # noqa: E402
from electrum.wallet import create_new_wallet  # noqa: E402
from electrum.transaction import Transaction  # noqa: E402
from electrum.bitcoin import address_to_script  # noqa: E402
from electrum.util import TxMinedInfo, create_and_start_event_loop  # noqa: E402

loop, stop_future, loop_thread = create_and_start_event_loop()
datadir = os.path.abspath(sys.argv[2])
os.makedirs(os.path.join(datadir, 'wallets'), exist_ok=True)
config = SimpleConfig({'electrum_path': datadir})
rnd = random.Random(42)


def fake_funding_tx(address: str, value: int) -> Transaction:
    """Minimal legacy tx: one fake input, one output to `address`."""
    spk = address_to_script(address)
    raw = struct.pack('<i', 2)                                    # version
    raw += b'\x01' + rnd.randbytes(32) + struct.pack('<I', 0)     # 1 input: fake prevout
    raw += b'\x00' + struct.pack('<I', 0xfffffffd)                # empty scriptSig, sequence
    raw += b'\x01' + struct.pack('<q', value) + bytes([len(spk)]) + spk
    raw += struct.pack('<I', 0)                                   # locktime
    return Transaction(raw.hex())


def make_wallet(name: str, seed_type: str, coins):
    path = os.path.join(datadir, 'wallets', name)
    if os.path.exists(path):
        os.remove(path)
    d = create_new_wallet(path=path, config=config, seed_type=seed_type, encrypt_file=False, gap_limit=len(coins) + 10)
    wallet = d['wallet']
    addrs = wallet.get_receiving_addresses()
    base_height = 960_000
    now = int(time.time())
    for i, (value, label) in enumerate(coins):
        tx = fake_funding_tx(addrs[i], value)
        h = base_height + i * 7
        wallet.adb.receive_tx_callback(tx, tx_height=h)
        wallet.adb.add_verified_tx(tx.txid(), TxMinedInfo(_height=h, conf=None, timestamp=now - (len(coins) - i) * 86400,
                                                          txpos=1, header_hash='00' * 32))
        if label:
            wallet.set_label(tx.txid(), label)
    wallet.db.put('stored_height', base_height + 10_000)  # offline wallets read their height from here
    wallet.save_db()
    utxos = wallet.get_utxos()
    print(f'{name}: {wallet.txin_type}, {len(utxos)} coins, {sum(u.value_sats() for u in utxos):,} sats, seed: {d["seed"]}')


segwit_coins = [(rnd.randint(1_500, 9_000), 'exchange withdrawal') for _ in range(11)] + \
               [(rnd.randint(800, 6_000), 'shop sales') for _ in range(7)] + \
               [(rnd.randint(2_000, 20_000), '') for _ in range(3)]
make_wallet('lowtide_fake_segwit', 'segwit', segwit_coins)
make_wallet('lowtide_fake_legacy', 'standard', [(12_000, 'old savings'), (3_300, 'old savings'), (900, '')])
loop.call_soon_threadsafe(stop_future.set_result, 1)
loop_thread.join(timeout=5)
