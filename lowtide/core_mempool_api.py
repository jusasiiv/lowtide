"""mempool.space REST API: URL building and response parsing (transport is injected)."""
from typing import Any, Dict, List, Optional, Sequence

from .core_histogram import Hist, normalize

# Lower bounds of the 39 fee bands in /api/v1/statistics (backend/src/api/statistics/statistics.ts).
# Band 0 is everything below 1 sat/vB.
STATISTICS_BANDS = [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 125, 150,
                    175, 200, 250, 300, 350, 400, 500, 600, 700, 800, 900, 1000, 1200, 1400, 1600, 1800, 2000]

PATHS = {
    'mempool': '/api/mempool',
    'mempool_blocks': '/api/v1/fees/mempool-blocks',
    'recommended': '/api/v1/fees/recommended',
    'blocks': '/api/v1/blocks',
    'statistics': '/api/v1/statistics/{period}',
    'feerates': '/api/v1/mining/blocks/fee-rates/{period}',
    'prices': '/api/v1/prices',
    'tx': '/api/tx/{txid}',
    'tx_status': '/api/tx/{txid}/status',
    'tx_hex': '/api/tx/{txid}/hex',
}


def url(base: str, name: str, **kw) -> str:
    return base.rstrip('/') + PATHS[name].format(**kw)


def parse_histogram(mempool_json: Dict[str, Any]) -> Hist:
    return normalize(mempool_json.get('fee_histogram') or [])


def parse_mempool_blocks(data: Sequence[dict]) -> List[dict]:
    out = []
    for b in data or []:
        try:
            out.append({
                'vsize': float(b.get('blockVSize') or 0),
                'ntx': int(b.get('nTx') or 0),
                'median_fee': float(b.get('medianFee') or 0),
                'fee_range': [float(x) for x in (b.get('feeRange') or [])],
                'total_fees': int(b.get('totalFees') or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def parse_blocks(data: Sequence[dict]) -> List[dict]:
    """Keep only what we use; newest first as served."""
    out = []
    for b in data or []:
        extras = b.get('extras') or {}
        if 'height' not in b:
            continue
        out.append({
            'height': b['height'],
            'timestamp': b.get('timestamp'),
            'weight': b.get('weight') or 0,
            'tx_count': b.get('tx_count') or 0,
            'extras': {
                'feeRange': extras.get('feeRange') or [],
                'medianFee': extras.get('medianFee'),
                'pool': {'name': ((extras.get('pool') or {}).get('name')) or '?'},
            },
        })
    return out


def parse_statistics(data: Sequence[dict]) -> List[dict]:
    """Sorted ascending by time; each point keeps added, vsizes, vbytes_per_second, min_fee."""
    pts = []
    for p in data or []:
        v = p.get('vsizes')
        if not v or len(v) != len(STATISTICS_BANDS) or not p.get('added'):
            continue
        pts.append({
            'added': int(p['added']),
            'vsizes': [float(x or 0) for x in v],
            'vps': float(p.get('vbytes_per_second') or 0),
            'min_fee': float(p.get('min_fee') or 0),
            'count': int(p.get('count') or 0),
        })
    pts.sort(key=lambda x: x['added'])
    return pts


def parse_recommended(data: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(v) for k, v in (data or {}).items() if isinstance(v, (int, float))}


def spike_levels(feerates: Sequence[dict]) -> Dict[int, float]:
    """Max block-group median fee rate per year, from /mining/blocks/fee-rates/{3y|all}."""
    import datetime
    out = {}  # type: Dict[int, float]
    for p in feerates or []:
        try:
            year = datetime.datetime.fromtimestamp(int(p['timestamp']), datetime.timezone.utc).year
            med = float(p.get('avgFee_50') or 0)
        except (KeyError, TypeError, ValueError):
            continue
        out[year] = max(out.get(year, 0.0), med)
    return out


# Fallback when offline: max block-group median per spike year (mempool.space data, Oct 2026).
SPIKE_FALLBACK = {2017: 936.0, 2021: 248.0, 2023: 467.0, 2024: 1190.0}
