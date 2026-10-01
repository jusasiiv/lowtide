"""Small formatting helpers shared by the Qt modules."""
import time
from typing import Optional

from electrum.i18n import _


def fmt_rate(x: Optional[float]) -> str:
    if x is None:
        return '—'
    if x >= 100:
        return f'{x:.0f}'
    if x >= 10:
        return f'{x:.0f}' if abs(x - round(x)) < 0.05 else f'{x:.1f}'
    s = f'{x:.1f}'
    return s[:-2] if s.endswith('.0') else s


def fmt_time(ts: float) -> str:
    """Local time, with the weekday when it is not today."""
    lt = time.localtime(ts)
    now = time.localtime()
    if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime('%H:%M', lt)
    return time.strftime('%a %H:%M', lt)


def fmt_window(w) -> str:
    """'Fri 02–13' or 'Sat 21–Mon 11' in local time."""
    a, b = time.localtime(w.start), time.localtime(w.end)
    if a.tm_yday == b.tm_yday or (b.tm_hour == 0 and b.tm_yday == a.tm_yday + 1):
        return time.strftime('%a %H', a) + '–' + time.strftime('%H', b)
    return time.strftime('%a %H', a) + '–' + time.strftime('%a %H', b)


def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    if s < 3600:
        return f'{max(1, s // 60)} min'
    if s < 48 * 3600:
        h = s / 3600
        return f'{h:.0f} h' if h >= 10 else f'{h:.1f} h'
    return f'{s / 86400:.1f} days'


def fmt_vmb(vb: float) -> str:
    return f'{vb / 1e6:.1f} vMB' if vb >= 100_000 else f'{vb / 1e3:.0f} kvB'


def fmt_sats_fiat(window, sats: int) -> str:
    """'1,234 sats (€0.92)' using the wallet window's fiat settings."""
    txt = f'{sats:,} sats'
    try:
        fiat = window.fx.format_amount_and_units(sats) if window.fx and window.fx.is_enabled() else ''
    except Exception:
        fiat = ''
    return f'{txt} ({fiat})' if fiat else txt
