#!/usr/bin/env python3
"""Record live mempool.space responses as test fixtures (stdlib only).

Usage: fetch_fixtures.py [outdir]   (default: tests/fixtures)
"""
import json
import os
import sys
import time
import urllib.request

BASE = 'https://mempool.space'
ENDPOINTS = {
    'mempool.json': '/api/mempool',
    'mempool_blocks.json': '/api/v1/fees/mempool-blocks',
    'recommended.json': '/api/v1/fees/recommended',
    'blocks.json': '/api/v1/blocks',
    'statistics_1w.json': '/api/v1/statistics/1w',
    'statistics_1m.json': '/api/v1/statistics/1m',
    'statistics_3m.json': '/api/v1/statistics/3m',
    'feerates_1m.json': '/api/v1/mining/blocks/fee-rates/1m',
    'feerates_3y.json': '/api/v1/mining/blocks/fee-rates/3y',
    'feerates_all.json': '/api/v1/mining/blocks/fee-rates/all',
    'prices.json': '/api/v1/prices',
    'accelerator_history.json': '/api/v1/services/accelerator/accelerations/history',
}


def get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'lowtide-fixtures'})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'tests', 'fixtures')
    os.makedirs(out, exist_ok=True)
    meta = {'fetched_at': int(time.time()), 'base': BASE}
    for name, path in ENDPOINTS.items():
        d = get(BASE + path)
        with open(os.path.join(out, name), 'w') as f:
            json.dump(d, f, separators=(',', ':'))
        print(f'{name}: {len(d) if hasattr(d, "__len__") else d}')
    # accelerator estimate for a transaction currently in the mempool
    recent = get(BASE + '/api/mempool/recent')
    txid = recent[0]['txid']
    est = get(BASE + '/api/v1/services/accelerator/estimate', json.dumps({'txInput': txid}).encode())
    with open(os.path.join(out, 'accelerator_estimate.json'), 'w') as f:
        json.dump(est, f, separators=(',', ':'))
    print('accelerator_estimate.json:', est.get('cost'), est.get('options'))
    with open(os.path.join(out, 'meta.json'), 'w') as f:
        json.dump(meta, f)


if __name__ == '__main__':
    main()
