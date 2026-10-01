"""On-disk cache and snapshot recorder (JSON, per Electrum data directory)."""
import json
import os
import time
from typing import Any, Dict, Iterator, List, Optional

from .histogram import Hist, bins, normalize


class Store:

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cache_path = os.path.join(data_dir, 'cache.json')
        self.history_path = os.path.join(data_dir, 'history.json')
        self.snapshots_path = os.path.join(data_dir, 'snapshots.jsonl')

    # --- generic json -------------------------------------------------

    @staticmethod
    def _read_json(path: str) -> Optional[Any]:
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
        os.replace(tmp, path)

    def load_cache(self) -> Dict[str, Any]:
        return self._read_json(self.cache_path) or {}

    def save_cache(self, data: Dict[str, Any]) -> None:
        self._write_json(self.cache_path, data)

    def load_history(self) -> Dict[str, Any]:
        return self._read_json(self.history_path) or {}

    def save_history(self, data: Dict[str, Any]) -> None:
        self._write_json(self.history_path, data)

    # --- snapshots ----------------------------------------------------

    @staticmethod
    def compact_histogram(hist: Hist) -> List[List[float]]:
        """0.1-step bins below 2 sat/vB, coarse above: small enough to record every 10 minutes."""
        return [[lo, v] for lo, hi, v in bins(normalize(hist)) if v > 0]

    def append_snapshot(self, *, ts: Optional[float] = None, hist: Hist, blocks: Optional[List[dict]] = None,
                        next_block_rate: Optional[float] = None, extra: Optional[dict] = None) -> dict:
        snap = {
            'ts': int(ts or time.time()),
            'hist': self.compact_histogram(hist),
            'next_block_rate': next_block_rate,
            'blocks': [
                {'height': b.get('height'), 'ts': b.get('timestamp'),
                 'feeRange': (b.get('extras') or {}).get('feeRange'),
                 'pool': ((b.get('extras') or {}).get('pool') or {}).get('name')}
                for b in (blocks or [])[:3]
            ],
        }
        if extra:
            snap.update(extra)
        with open(self.snapshots_path, 'a') as f:
            f.write(json.dumps(snap, separators=(',', ':')) + '\n')
        return snap

    def iter_snapshots(self, since_ts: float = 0) -> Iterator[dict]:
        try:
            with open(self.snapshots_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                    except ValueError:
                        continue
                    if s.get('ts', 0) >= since_ts:
                        yield s
        except OSError:
            return

    def last_snapshots(self, n: int = 2) -> List[dict]:
        snaps = list(self.iter_snapshots())
        return snaps[-n:]

    @staticmethod
    def expand_compact(compact: List[List[float]]) -> Hist:
        """Turn a compact snapshot back into a histogram at bin resolution."""
        return normalize([(lo, v) for lo, v in compact])
