"""Send-later planning: put each queued item into the best low-tide window before its deadline."""
import time
from typing import Callable, Dict, List, Optional, Sequence

from .forecast import Forecast, Window, HOUR
from .histogram import FLOOR_RATE, quantize

MARGIN_S = 3 * 3600          # a window must end this long before the deadline
DEADLINE_GUARD_S = 2 * 3600  # open rescue when a sent batch is still unconfirmed this close to its deadline

RateForDeadline = Callable[[float], Optional[float]]  # seconds -> rate


def plan_items(items: Sequence[dict], forecast: Optional[Forecast], now: float, *, floor_rate: float,
               rate_for_deadline: RateForDeadline, next_block_rate: Optional[float], demo: Optional[dict] = None) -> List[dict]:
    """Returns the items with planned_start / planned_rate / plan_note filled in (queued items only)."""
    demo = demo or {}
    out = []
    for it in items:
        it = dict(it)
        if it.get('status') != 'queued':
            out.append(it)
            continue
        deadline = it.get('deadline')
        if demo.get('deadline_soon') and deadline:
            deadline = now + 1800
        cap = it.get('max_feerate')
        is_consol = it.get('kind') in ('consolidation', 'migration')
        windows = list(forecast.windows) if forecast else []
        usable = []
        for w in windows:
            if w.end <= now:
                continue
            if deadline is not None and w.start + HOUR > deadline - MARGIN_S:
                continue
            usable.append(w)
        if demo.get('low_tide_now'):
            start = (now // HOUR) * HOUR
            usable.insert(0, Window(start=int(start), end=int(start + 4 * HOUR), rate=min([w.rate for w in windows] + [floor_rate + 0.2])))
        chosen = None
        if usable:
            chosen = min(usable, key=lambda w: (w.rate, w.start))
        if chosen is not None:
            rate = floor_rate if is_consol else max(FLOOR_RATE, chosen.rate)
            it['planned_start'] = int(max(now, chosen.start)) if chosen.start <= now < chosen.end else int(chosen.start)
            it['plan_note'] = 'low tide'
        elif deadline is not None:
            secs = max(600.0, deadline - now - 1800)
            rate = rate_for_deadline(secs) or next_block_rate or 1.0
            it['planned_start'] = int(now)
            it['plan_note'] = 'no low tide before the deadline: send now'
        else:
            rate = floor_rate if is_consol else (next_block_rate or 1.0)
            it['planned_start'] = int(now)
            it['plan_note'] = 'no low tide in the forecast: send now'
        if cap is not None and rate > cap:
            rate = cap
            it['plan_note'] += ' (capped at your max rate)'
        it['planned_rate'] = quantize(max(FLOOR_RATE, rate))
        out.append(it)
    return out


def due_items(items: Sequence[dict], now: float, *, demo: Optional[dict] = None) -> List[dict]:
    demo = demo or {}
    return [it for it in items if it.get('status') == 'queued' and (demo.get('low_tide_now') or (it.get('planned_start') or 0) <= now)]


def overdue_items(items: Sequence[dict], now: float) -> List[dict]:
    """Queued items whose deadline has passed."""
    return [it for it in items if it.get('status') == 'queued' and it.get('deadline') and it['deadline'] < now]


def guard_items(items: Sequence[dict], now: float, unconfirmed_txids: set, *, demo: Optional[dict] = None) -> List[dict]:
    """Sent items that are still unconfirmed as their deadline nears."""
    demo = demo or {}
    out = []
    for it in items:
        if it.get('status') != 'sent' or not it.get('txid') or it['txid'] not in unconfirmed_txids:
            continue
        dl = it.get('deadline')
        if demo.get('deadline_soon') and dl:
            dl = now + 1800
        if dl and dl - now < DEADLINE_GUARD_S:
            out.append(it)
    return out


def batch_rate(items: Sequence[dict]) -> float:
    """One batch pays the highest planned rate among its items (so every deadline is met)."""
    rates = [it.get('planned_rate') or FLOOR_RATE for it in items]
    return max(rates) if rates else FLOOR_RATE
