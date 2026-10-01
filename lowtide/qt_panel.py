"""Forecast panel: 7-day tide chart, rate checker, deadline planner, pile view, method."""
import math
import time
from typing import TYPE_CHECKING, Callable, List, Optional

from PyQt6.QtCore import Qt, QTimer, QDateTime, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF, QFont, QDoubleValidator
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget, QWidget, QLineEdit,
                             QPushButton, QDateTimeEdit, QGridLayout, QToolTip, QSizePolicy, QFrame)

from electrum.i18n import _
from electrum.gui.qt.util import WWLabel, Buttons, CloseButton

from .core import histogram as H
from .core.forecast import Forecast, Window, HOUR
from .fmt import fmt_rate, fmt_time, fmt_window, fmt_duration, fmt_vmb, fmt_sats_fiat

if TYPE_CHECKING:
    from .service import State


def _color(name: str) -> QColor:
    return {
        'band': QColor(70, 140, 200, 60),
        'median': QColor(30, 100, 170),
        'lowtide': QColor(60, 180, 90, 50),
        'lowtide_line': QColor(40, 150, 70),
        'now': QColor(220, 90, 40),
        'grid': QColor(150, 150, 150, 70),
        'text': QColor(110, 110, 110),
        'pile': QColor(120, 140, 170),
        'jump': QColor(230, 140, 30),
        'above': QColor(30, 100, 170),
        'threshold': QColor(40, 150, 70, 160),
    }[name]


class TideChart(QWidget):
    """7-day hourly forecast with uncertainty band, low-tide shading and a now marker."""

    def __init__(self):
        QWidget.__init__(self)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.forecast = None  # type: Optional[Forecast]
        self.now = 0
        self.history = []  # type: List[tuple]  # (ts, rate) past week
        self._geom = None

    def set_data(self, forecast: Optional[Forecast], now: int, history=None):
        self.forecast = forecast
        self.now = now
        self.history = list(history or [])
        self.update()

    def _y(self, rate: float) -> float:
        top, bottom, ymin, ymax = self._geom[2], self._geom[3], self._geom[4], self._geom[5]
        rate = max(ymin, min(ymax, rate))
        return bottom - (math.log(rate) - math.log(ymin)) / (math.log(ymax) - math.log(ymin)) * (bottom - top)

    def _x(self, ts: float) -> float:
        left, right, t0, t1 = self._geom[0], self._geom[1], self._geom[6], self._geom[7]
        return left + (ts - t0) / float(t1 - t0) * (right - left)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 44, w - 10, 12, h - 24
        fc = self.forecast
        if fc is None or not fc.horizon:
            p.setPen(_color('text'))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _('No forecast yet. Waiting for data…'))
            return
        t0 = min([fc.horizon[0].ts] + [ts for ts, _ in self.history]) if self.history else fc.horizon[0].ts
        t1 = fc.horizon[-1].ts + HOUR
        vals = [p_.lo for p_ in fc.horizon] + [p_.hi for p_ in fc.horizon] + [r for _, r in self.history]
        ymin = max(0.1, min(vals) / 1.6)
        ymax = max(vals) * 1.6
        if ymax / ymin < 8:
            ymax = ymin * 8
        self._geom = (left, right, top, bottom, ymin, ymax, t0, t1)
        # grid: y ticks
        p.setFont(QFont(p.font().family(), 8))
        for tick in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
            if ymin <= tick <= ymax:
                y = self._y(tick)
                p.setPen(QPen(_color('grid'), 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(left, y), QPointF(right, y))
                p.setPen(_color('text'))
                p.drawText(QRectF(0, y - 7, left - 4, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt_rate(tick))
        # day separators (local midnight)
        lt = time.localtime(t0)
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        d = day_start
        while d < t1:
            if d >= t0:
                x = self._x(d)
                p.setPen(QPen(_color('grid'), 1))
                p.drawLine(QPointF(x, top), QPointF(x, bottom))
            nxt = d + 86400
            mid = max(d, t0) + (min(nxt, t1) - max(d, t0)) / 2
            if min(nxt, t1) - max(d, t0) > 6 * HOUR:
                p.setPen(_color('text'))
                p.drawText(QRectF(self._x(mid) - 30, bottom + 2, 60, 18), Qt.AlignmentFlag.AlignCenter, time.strftime('%a %d', time.localtime(mid)))
            d = nxt
        # low-tide windows
        for wdw in fc.windows:
            x0, x1 = self._x(wdw.start), self._x(wdw.end)
            p.fillRect(QRectF(x0, top, x1 - x0, bottom - top), _color('lowtide'))
        # threshold
        p.setPen(QPen(_color('threshold'), 1, Qt.PenStyle.DashLine))
        yt = self._y(fc.threshold)
        p.drawLine(QPointF(left, yt), QPointF(right, yt))
        # band polygon
        poly = QPolygonF()
        for pt in fc.horizon:
            poly.append(QPointF(self._x(pt.ts + HOUR / 2), self._y(pt.hi)))
        for pt in reversed(fc.horizon):
            poly.append(QPointF(self._x(pt.ts + HOUR / 2), self._y(pt.lo)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_color('band')))
        p.drawPolygon(poly)
        # history line (past week)
        if self.history:
            p.setPen(QPen(_color('text'), 1.2))
            pts = [QPointF(self._x(ts + HOUR / 2), self._y(r)) for ts, r in self.history]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
        # median line
        p.setPen(QPen(_color('median'), 2))
        pts = [QPointF(self._x(pt.ts + HOUR / 2), self._y(pt.median)) for pt in fc.horizon]
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        # now marker
        if t0 <= self.now <= t1:
            x = self._x(self.now)
            p.setPen(QPen(_color('now'), 2))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            p.setPen(_color('now'))
            p.drawText(QRectF(x + 3, top, 60, 14), Qt.AlignmentFlag.AlignLeft, _('now'))
        # axis frame
        p.setPen(QPen(_color('grid'), 1))
        p.drawLine(QPointF(left, bottom), QPointF(right, bottom))
        p.setPen(_color('text'))
        p.drawText(QRectF(left, 0, 200, 12), Qt.AlignmentFlag.AlignLeft, 'sat/vB (next block)')

    def mouseMoveEvent(self, ev):
        if not self._geom or not self.forecast:
            return
        left, right, top, bottom, ymin, ymax, t0, t1 = self._geom
        x = ev.position().x()
        if x < left or x > right:
            QToolTip.hideText()
            return
        ts = t0 + (x - left) / (right - left) * (t1 - t0)
        pt = self.forecast.point_at(int(ts))
        if pt is None:
            hist = [(t, r) for t, r in self.history if t <= ts < t + HOUR]
            if hist:
                QToolTip.showText(ev.globalPosition().toPoint(), f"{fmt_time(hist[0][0])}: {fmt_rate(hist[0][1])} sat/vB (observed)", self)
            return
        wdw = self.forecast.current_window(int(ts))
        txt = f"{fmt_time(pt.ts)}: ≈{fmt_rate(pt.median)} sat/vB ({fmt_rate(pt.lo)}–{fmt_rate(pt.hi)})"
        if wdw:
            txt += '\n' + _('low tide')
        QToolTip.showText(ev.globalPosition().toPoint(), txt, self)


class PileChart(QWidget):
    """Mempool depth by fee rate: 0.1-step bins below 2 sat/vB, coarser above."""

    def __init__(self):
        QWidget.__init__(self)
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self.bins = []  # type: List[tuple]
        self.jump = None  # type: Optional[H.PileJump]
        self.next_rate = None
        self._bars = []

    def set_data(self, hist: H.Hist, jump: Optional[H.PileJump], next_rate: Optional[float]):
        self.bins = H.bins(hist) if hist else []
        self.jump = jump
        self.next_rate = next_rate
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 44, w - 10, 14, h - 34
        self._bars = []
        if not self.bins:
            p.setPen(_color('text'))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _('No mempool data yet…'))
            return
        vmax = max(v for _, _, v in self.bins) or 1
        n = len(self.bins)
        bw = (right - left) / float(n)
        p.setFont(QFont(p.font().family(), 8))
        for tick in (0.25, 0.5, 0.75, 1.0):
            y = bottom - tick * (bottom - top)
            p.setPen(QPen(_color('grid'), 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(left, y), QPointF(right, y))
            p.setPen(_color('text'))
            p.drawText(QRectF(0, y - 7, left - 4, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt_vmb(vmax * tick))
        for i, (lo, hi, v) in enumerate(self.bins):
            x = left + i * bw
            bh = (v / vmax) * (bottom - top) if v else 0
            if self.jump and abs(lo - self.jump.rate) < 1e-6:
                col = _color('jump')
            elif hi <= 1.0 + 1e-9:
                col = _color('pile')
            else:
                col = _color('above')
            rect = QRectF(x + 1, bottom - bh, max(1.0, bw - 2), bh)
            p.fillRect(rect, col)
            self._bars.append((x, x + bw, lo, hi, v))
            label = None
            if abs(lo - round(lo)) < 1e-6 and (lo <= 2.0 or hi == math.inf or lo in (5, 10, 20, 50, 100)):
                label = fmt_rate(lo) + ('+' if hi == math.inf else '')
            elif lo in (0.5, 1.5):
                label = fmt_rate(lo)
            if label:
                p.setPen(_color('text'))
                p.drawText(QRectF(x - 20, bottom + 2, bw + 40, 14), Qt.AlignmentFlag.AlignCenter, label)
        # Electrum minimum (1 sat/vB) marker
        for i, (lo, hi, v) in enumerate(self.bins):
            if abs(lo - 1.0) < 1e-6:
                x = left + i * bw
                p.setPen(QPen(_color('median'), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(x, top), QPointF(x, bottom))
                p.setPen(_color('median'))
                p.drawText(QRectF(x + 2, top - 2, 160, 14), Qt.AlignmentFlag.AlignLeft, _("Electrum's minimum (1 sat/vB)"))
                break
        p.setPen(QPen(_color('grid'), 1))
        p.drawLine(QPointF(left, bottom), QPointF(right, bottom))
        p.setPen(_color('text'))
        p.drawText(QRectF(left, bottom + 16, right - left, 14), Qt.AlignmentFlag.AlignCenter, _('fee rate (sat/vB) → mempool vsize per bin'))

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        for x0, x1, lo, hi, v in self._bars:
            if x0 <= x < x1:
                rng = f"{fmt_rate(lo)}–{fmt_rate(hi)}" if hi != math.inf else f"≥{fmt_rate(lo)}"
                QToolTip.showText(ev.globalPosition().toPoint(), f"{rng} sat/vB: {fmt_vmb(v)}", self)
                return
        QToolTip.hideText()


class ForecastPanel(QDialog):
    """Non-modal window, one per wallet window."""

    def __init__(self, plugin, main_window):
        QDialog.__init__(self, parent=None)
        self.plugin = plugin
        self.main_window = main_window
        self.state = None  # type: Optional[State]
        self.setWindowTitle('LowTide — ' + _('Tide forecast'))
        self.setWindowIcon(plugin.icon())
        self.setMinimumSize(760, 560)
        vbox = QVBoxLayout(self)

        self.head_now = QLabel('')
        f = self.head_now.font(); f.setPointSize(f.pointSize() + 3); f.setBold(True)
        self.head_now.setFont(f)
        self.head_tide = QLabel('')
        self.head_meta = QLabel('')
        self.head_meta.setStyleSheet('color: gray')
        self.head_warn = WWLabel('')
        self.head_warn.setStyleSheet('color: #b06000')
        self.head_warn.hide()
        vbox.addWidget(self.head_now)
        vbox.addWidget(self.head_tide)
        vbox.addWidget(self.head_meta)
        vbox.addWidget(self.head_warn)

        self.chart = TideChart()
        vbox.addWidget(self.chart, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_checker(), _('Rate & deadline'))
        self.tabs.addTab(self._build_pile(), _('Pile view'))
        self.tabs.addTab(self._build_method(), _('Method'))
        vbox.addWidget(self.tabs)

        hbox = QHBoxLayout()
        self.refresh_btn = QPushButton(_('Refresh'))
        self.refresh_btn.clicked.connect(self.plugin.service.refresh_now)
        hbox.addWidget(self.refresh_btn)
        settings_btn = QPushButton(_('Settings'))
        settings_btn.clicked.connect(lambda: self.plugin.settings_dialog(self))
        hbox.addWidget(settings_btn)
        hbox.addStretch(1)
        hbox.addWidget(CloseButton(self))
        vbox.addLayout(hbox)

        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._refresh_derived)
        self._timer.start()

    # --- tabs ---------------------------------------------------------

    def _build_checker(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        grid.addWidget(QLabel(_('If I pay')), 0, 0)
        self.rate_e = QLineEdit()
        self.rate_e.setValidator(QDoubleValidator(0.1, 10000, 1))
        self.rate_e.setPlaceholderText('0.4')
        self.rate_e.setFixedWidth(80)
        self.rate_e.textChanged.connect(self._refresh_derived)
        grid.addWidget(self.rate_e, 0, 1)
        grid.addWidget(QLabel('sat/vB'), 0, 2)
        self.rate_result = WWLabel('')
        grid.addWidget(self.rate_result, 0, 3)
        self.rate_apply = QPushButton(_('Use for next send'))
        self.rate_apply.clicked.connect(lambda: self._apply(self._rate_input()))
        grid.addWidget(self.rate_apply, 0, 4)

        grid.addWidget(QLabel(_('Confirmed by')), 1, 0)
        self.deadline_e = QDateTimeEdit(QDateTime.currentDateTime().addSecs(24 * 3600))
        self.deadline_e.setCalendarPopup(True)
        self.deadline_e.setDisplayFormat('ddd dd MMM HH:mm')
        self.deadline_e.dateTimeChanged.connect(self._refresh_derived)
        grid.addWidget(self.deadline_e, 1, 1, 1, 2)
        self.deadline_result = WWLabel('')
        grid.addWidget(self.deadline_result, 1, 3)
        self.deadline_apply = QPushButton(_('Use this rate'))
        self.deadline_apply.clicked.connect(lambda: self._apply(self._deadline_rate))
        grid.addWidget(self.deadline_apply, 1, 4)
        self._deadline_rate = None
        grid.setColumnStretch(3, 1)
        return w

    def _build_pile(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.pile_chart = PileChart()
        v.addWidget(self.pile_chart, 1)
        hb = QHBoxLayout()
        self.pile_label = WWLabel('')
        hb.addWidget(self.pile_label, 1)
        self.pile_apply = QPushButton('')
        self.pile_apply.clicked.connect(lambda: self._apply(self.state.pile.rate if self.state and self.state.pile else None))
        hb.addWidget(self.pile_apply)
        v.addLayout(hb)
        return w

    def _build_method(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.method_label = WWLabel('')
        self.method_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.method_label)
        v.addStretch(1)
        return w

    # --- updates ------------------------------------------------------

    def update_state(self, st: 'State'):
        self.state = st
        now = st.now()
        fc = st.forecast
        if st.next_block_rate is not None:
            pct = f" · {_('cheaper than')} {fc.now_percentile * 100:.0f}% {_('of the past week')}" if fc else ''
            self.head_now.setText(f"{_('Now')}: {fmt_rate(st.next_block_rate)} sat/vB {_('for the next block')}{pct}")
        else:
            self.head_now.setText(_('Waiting for mempool data…'))
        cur, nxt = st.current_window(), st.next_window()
        if cur:
            self.head_tide.setText(f"🌊 {_('Low tide now')}: ≈{fmt_rate(cur.rate)} sat/vB {_('until')} {fmt_time(cur.end)}")
        elif nxt:
            self.head_tide.setText(f"{_('Next low tide')}: {fmt_window(nxt)} ≈{fmt_rate(nxt.rate)} sat/vB ({_('in')} {fmt_duration(nxt.start - now)})")
        else:
            self.head_tide.setText(_('No low tide found in the next 7 days.'))
        src = {'mempool': self.plugin.config.LOWTIDE_MEMPOOL_URL, 'server': _('Electrum server'), 'cache': _('cache')}.get(st.hist_source, st.hist_source)
        meta = f"{_('Data as of')} {fmt_time(st.data_as_of) if st.data_as_of else '—'} · {src}"
        if fc:
            meta += f" · {fc.n_weeks} {_('weeks of history')}"
        if st.offline:
            meta += f" · {_('offline, showing cached data')}" + (f" ({st.last_error})" if st.last_error else '')
        self.head_meta.setText(meta)
        warn = self.plugin.relay_warning_text(self.main_window)
        self.head_warn.setText(warn or '')
        self.head_warn.setVisible(bool(warn))
        self.chart.set_data(fc, now, [(p.ts, p.rate) for p in st.past_week])
        self.pile_chart.set_data(st.hist, st.pile, st.next_block_rate)
        if st.pile:
            e = st.eta(st.pile.rate)
            eta_txt = self.plugin.fmt_eta(e)
            self.pile_label.setText(
                f"<b>{fmt_rate(st.pile.rate)} sat/vB</b>: {_('ahead of')} {fmt_vmb(st.pile.ahead_vb)}, {_('behind')} {fmt_vmb(st.pile.behind_vb)} "
                f"({_('paying')} {fmt_rate(st.pile.ceiling_rate)} sat/vB: {_('behind')} {fmt_vmb(st.pile.ceiling_behind_vb)}). {eta_txt}<br>"
                f"{_('Mempool')}: {fmt_vmb(H.total_vsize(st.hist))} {_('total')}, {fmt_vmb(H.sub1_vsize(st.hist))} {_('below 1 sat/vB')}, "
                f"{_('minimum')} {fmt_rate(st.mempool_min_fee)} sat/vB.")
            self.pile_apply.setText(_('Use {} sat/vB for next send').format(fmt_rate(st.pile.rate)))
            self.pile_apply.setEnabled(True)
        else:
            self.pile_label.setText(_('No floor pile right now: the mempool holds less than 1 vMB below 1 sat/vB.'))
            self.pile_apply.setText(_('Use for next send'))
            self.pile_apply.setEnabled(False)
        bt = (st.backtest or {}).get('headline') or _('Backtest pending (needs 3 months of history).')
        self.method_label.setText(
            f"<b>{_('Method')}</b><br>{fc.method if fc else ''}<br><br>"
            f"<b>{_('Accuracy')}</b><br>{bt}<br><br>"
            f"<b>{_('Resolution below 1 sat/vB')}</b><br>"
            + _("mempool history cannot resolve below 1 sat/vB, so sub-1 advice comes from the live fee histogram, "
                "and LowTide records its own 10-minute snapshots to build sub-1 history over time.") + "<br><br>"
            f"<b>{_('Privacy')}</b><br>"
            + _("Only aggregate network data is fetched (through Electrum's proxy, so Tor applies). Nothing about your "
                "wallet leaves this machine. A txid is sent to mempool.space only when you open the accelerator option."))
        self._refresh_derived()

    def _rate_input(self) -> Optional[float]:
        try:
            return float(self.rate_e.text().replace(',', '.'))
        except ValueError:
            return None

    def _refresh_derived(self):
        st = self.state
        if st is None or not st.hist:
            return
        r = self._rate_input()
        if r is None or r < 0.1:
            self.rate_result.setText(_('Enter a rate to see when it would confirm.'))
            self.rate_apply.setEnabled(False)
        else:
            e = st.eta(r)
            self.rate_result.setText(self.plugin.fmt_eta(e, verbose=True))
            self.rate_apply.setEnabled(True)
        deadline = self.deadline_e.dateTime().toSecsSinceEpoch()
        secs = deadline - st.now()
        if secs <= 600:
            self.deadline_result.setText(_('Deadline is in the past.'))
            self._deadline_rate = None
            self.deadline_apply.setEnabled(False)
            return
        e = st.rate_for_deadline(secs)
        if e is None:
            self.deadline_result.setText(_('No rate found that confirms in time.'))
            self._deadline_rate = None
            self.deadline_apply.setEnabled(False)
            return
        self._deadline_rate = e.rate
        txt = f"{_('Cheapest safe rate now')}: <b>{fmt_rate(e.rate)} sat/vB</b> ({self.plugin.fmt_eta(e)})"
        best = st.best_send_time(deadline)
        if best and best.median < e.rate - 1e-9:
            txt += f"<br>{_('Or wait')}: {fmt_time(best.ts)} {_('at')} ≈{fmt_rate(best.median)} sat/vB ({_('forecast')})"
        self.deadline_result.setText(txt)
        self.deadline_apply.setEnabled(True)

    def _apply(self, rate: Optional[float]):
        if rate is None:
            return
        self.plugin.apply_rate(self.main_window, rate)
