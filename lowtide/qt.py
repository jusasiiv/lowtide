"""Qt entry point for LowTide."""
import json
import time
from functools import partial
from typing import TYPE_CHECKING, Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import (QToolButton, QStatusBar, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QCheckBox, QPushButton, QGridLayout, QDialog, QWidget)

from electrum.i18n import _
from electrum.network import Network
from electrum.plugin import hook
from electrum.gui.qt.util import (WindowModalDialog, Buttons, CloseButton, OkButton, CancelButton,
                                  read_QIcon_from_bytes, WWLabel)

from .lowtide import LowTidePlugin
from . import qt_notify
from .core.version import CORE_VERSION
from .core.store import Store
from .core import eta as E
from .service import LowTideService, State
from .fmt import fmt_rate, fmt_time, fmt_window, fmt_duration
from .qt_panel import ForecastPanel

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet

RELAY_SUB1_SAT_PER_KVB = 100  # a server that relays 0.1 sat/vB reports exactly 100 sat/kvB


class StatusButton(QToolButton):
    """Flat text button in the status bar: the always-visible tide indicator."""

    def __init__(self):
        QToolButton.__init__(self)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_state(text='LowTide', tooltip=_('Fee tide forecast: loading…'))

    def set_state(self, *, text: str, tooltip: str, color: Optional[str] = None, bold: bool = False):
        self.setText('⛵ ' + text)
        self.setToolTip(tooltip)
        style = []
        if color:
            style.append(f'color: {color};')
        if bold:
            style.append('font-weight: bold;')
        self.setStyleSheet('QToolButton { ' + ' '.join(style) + ' }' if style else '')


class SendTabRow(QWidget):
    """One line in the Send tab: the live LowTide suggestion and a button to use it."""

    def __init__(self, plugin):
        QWidget.__init__(self)
        self.plugin = plugin
        self.rate = None
        hb = QHBoxLayout(self)
        hb.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(_('LowTide: waiting for data…'))
        hb.addWidget(self.label, 1)
        self.btn = QPushButton(_('Use'))
        self.btn.setEnabled(False)
        self.btn.clicked.connect(self._use)
        hb.addWidget(self.btn)

    def _use(self):
        if self.rate is not None:
            self.plugin.apply_rate(self.window(), self.rate)

    def update_state(self, st: State):
        if st.pile:
            e = st.eta(st.pile.rate)
            self.rate = st.pile.rate
            self.label.setText(f"LowTide: {fmt_rate(st.pile.rate)} sat/vB {_('jumps the pile')} · {self.plugin.fmt_eta(e)}")
            self.btn.setText(_('Use {} sat/vB').format(fmt_rate(st.pile.rate)))
            self.btn.setEnabled(True)
        elif st.next_block_rate is not None:
            self.rate = st.next_block_rate
            self.label.setText(f"LowTide: {_('next block')} ≈{fmt_rate(st.next_block_rate)} sat/vB")
            self.btn.setText(_('Use {} sat/vB').format(fmt_rate(st.next_block_rate)))
            self.btn.setEnabled(True)


class QtBridge(QObject):
    state_updated = pyqtSignal(object)


class LowTideAlert(QDialog):
    """Non-modal in-app cue paired with the desktop notification."""

    def __init__(self, plugin, main_window, title: str, text: str, actions: List[tuple]):
        QDialog.__init__(self, parent=main_window)
        self.setWindowTitle('LowTide — ' + title)
        self.setWindowIcon(plugin.icon())
        self.setModal(False)
        v = QVBoxLayout(self)
        v.addWidget(WWLabel(text))
        hb = QHBoxLayout()
        for label, fn in actions:
            b = QPushButton(label)
            b.clicked.connect(lambda _checked=False, fn=fn: (fn(), self.close()))
            hb.addWidget(b)
        hb.addStretch(1)
        hb.addWidget(CloseButton(self))
        v.addLayout(hb)


class Plugin(LowTidePlugin):

    def __init__(self, parent, config, name):
        LowTidePlugin.__init__(self, parent, config, name)
        self._status_buttons = {}  # type: Dict[int, StatusButton]  # id(QStatusBar) -> button
        self._send_rows = []       # type: List[SendTabRow]
        self._windows = {}         # type: Dict[ElectrumWindow, dict]
        self._icon_bytes = self.read_file('lowtide.png')
        self._notified_path = None
        self._notified = set()
        self._alerts = []
        self._load_notified()
        self.logger.info(f'LowTide core {CORE_VERSION}')
        self.bridge = QtBridge()
        self.bridge.state_updated.connect(self._on_state, Qt.ConnectionType.QueuedConnection)
        self.service = LowTideService(
            store=Store(self.data_dir), http_get=self._http_get, base_url_fn=self._base_url,
            poll_minutes_fn=lambda: self.config.LOWTIDE_POLL_MINUTES, privacy_mode_fn=lambda: bool(self.config.LOWTIDE_PRIVACY_MODE),
            server_histogram_fn=self._server_histogram, logger=self.logger)
        self.service.add_listener(lambda st: self.bridge.state_updated.emit(st))
        self.service.start()

    def on_close(self):
        self.service.stop()

    def icon(self):
        return read_QIcon_from_bytes(self._icon_bytes)

    # --- transport ----------------------------------------------------

    def _http_get(self, url: str, timeout: float):
        network = Network.get_instance()
        if network is None:
            raise Exception('offline: no network')
        text = Network.send_http_on_proxy('get', url, timeout=timeout)
        return json.loads(text)

    def _base_url(self) -> str:
        network = Network.get_instance()
        is_tor = bool(getattr(network, 'is_proxy_tor', False)) if network else False
        return self.mempool_base_url(is_tor=is_tor)

    def _server_histogram(self):
        network = Network.get_instance()
        if network is None:
            return None
        data = getattr(network.mempool_fees, '_data', None)
        return list(data) if data else None

    @property
    def state(self) -> State:
        return self.service.state

    # --- hooks --------------------------------------------------------

    @hook
    def create_status_bar(self, sb: QStatusBar):
        btn = StatusButton()
        btn.clicked.connect(partial(self._on_status_clicked, btn))
        sb.addWidget(btn)
        self._status_buttons[id(sb)] = btn
        self._update_status_button(btn, self.state)

    @hook
    def create_send_tab(self, grid: QGridLayout):
        row = SendTabRow(self)
        r = grid.rowCount()
        lbl = QLabel('⛵')
        lbl.setToolTip('LowTide')
        grid.addWidget(lbl, r, 0)
        grid.addWidget(row, r, 1, 1, 4)
        self._send_rows.append(row)
        row.update_state(self.state)

    @hook
    def init_menubar(self, window: 'ElectrumWindow'):
        m = window.tools_menu.addMenu('LowTide')
        m.setIcon(self.icon())
        m.addAction(_('Tide forecast'), lambda: self.open_panel(window))
        m.addSeparator()
        m.addAction(_('Settings'), lambda: self.settings_dialog(window))

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        if window is None:
            return
        self._windows[window] = {'wallet': wallet, 'panel': None}

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet'):
        for window in [w for w, st in self._windows.items() if st.get('wallet') is wallet]:
            self._close_window(window)

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        self._close_window(window)

    def _close_window(self, window):
        st = self._windows.pop(window, None)
        if st and st.get('panel'):
            try:
                st['panel'].close()
            except Exception:
                pass
        try:
            self._status_buttons.pop(id(window.statusBar()), None)
        except Exception:
            pass
        self._send_rows = [r for r in self._send_rows if r.window() is not window]

    # --- state → UI ---------------------------------------------------

    def _on_state(self, st: State):
        for btn in list(self._status_buttons.values()):
            try:
                self._update_status_button(btn, st)
            except RuntimeError:
                pass
        for row in list(self._send_rows):
            try:
                row.update_state(st)
            except RuntimeError:
                pass
        for window, wst in list(self._windows.items()):
            panel = wst.get('panel')
            if panel is not None and panel.isVisible():
                panel.update_state(st)
        self._maybe_notify_low_tide(st)

    def _update_status_button(self, btn: StatusButton, st: State):
        if st.next_block_rate is None:
            btn.set_state(text='LowTide', tooltip=_('Fee tide forecast: waiting for data…'))
            return
        window = btn.window()
        relay_warn = self.relay_warning_text(window if window in self._windows else None)
        cur, nxt = st.current_window(), st.next_window()
        text = f"{fmt_rate(st.next_block_rate)} sat/vB"
        tip = [f"{_('Next block')}: {fmt_rate(st.next_block_rate)} sat/vB"]
        color, bold = None, False
        if cur:
            text += f" · {_('low tide now')} ≈{fmt_rate(cur.rate)} {_('until')} {fmt_time(cur.end)}"
            tip.append(_('Low tide: a good time for consolidation and non-urgent sends.'))
            color, bold = '#1e8f3e', True
        elif nxt:
            text += f" · {_('low tide')} {fmt_window(nxt)} ≈{fmt_rate(nxt.rate)}"
            tip.append(f"{_('Next low tide')} {_('in')} {fmt_duration(nxt.start - st.now())}")
        if st.pile:
            tip.append(f"{_('Pile-jump rate')}: {fmt_rate(st.pile.rate)} sat/vB")
        if relay_warn:
            text += ' · ' + _('server blocks sub-1')
            tip.append(relay_warn)
            color = '#b06000'
        if st.offline:
            tip.append(_('Offline: showing cached data as of') + ' ' + (fmt_time(st.data_as_of) if st.data_as_of else '—'))
        tip.append(_('Click for the tide forecast.'))
        btn.set_state(text=text, tooltip='\n'.join(tip), color=color, bold=bold)

    def relay_warning_text(self, window) -> Optional[str]:
        """Warn when the connected Electrum server refuses fees below 1 sat/vB."""
        network = Network.get_instance()
        if network is None or network.relay_fee is None:
            return None
        if network.relay_fee <= RELAY_SUB1_SAT_PER_KVB:
            return None
        return (_('Your Electrum server only relays fees from {} sat/vB, so Electrum will block sub-1 sat/vB sends ("below relay fee"). ')
                .format(fmt_rate(network.relay_fee / 1000))
                + _('Switch to a server that relays 0.1 sat/vB: Tools ▸ Network, untick "Select server automatically" and pick another one; most default servers relay down to 0.1.'))

    def fmt_eta(self, e: Optional[E.Eta], verbose: bool = False) -> str:
        if e is None:
            return _('ETA unknown')
        now = time.time()
        if e.median_s is None:
            return _('not before the tide turns (more than 7 days)')
        txt = f"{_('likely by')} {fmt_time(now + e.median_s)} ({fmt_duration(e.median_s)})"
        if e.p90_s is not None and e.p90_s > e.median_s * 1.3 + 600:
            txt += f", {_('90% by')} {fmt_time(now + e.p90_s)}"
        if e.note == 'when the tide turns':
            txt += ' · ' + _('when the tide turns')
        if verbose:
            txt += f"<br>{_('Queue ahead')}: {e.queue_vb / 1e6:.2f} vMB · {_('arriving above this rate')}: {e.arrival_vb_per_s:.0f} vB/s"
            if e.blocks:
                txt += f" · {e.blocks} {_('blocks')}"
        return txt

    # --- actions ------------------------------------------------------

    def _on_status_clicked(self, btn: StatusButton):
        self.open_panel(btn.window())

    def open_panel(self, window):
        wst = self._windows.get(window)
        if wst is None:
            # status bar clicked before load_wallet, or a non-wallet window
            for w in self._windows:
                window, wst = w, self._windows[w]
                break
            else:
                return
        panel = wst.get('panel')
        if panel is None:
            panel = ForecastPanel(self, window)
            wst['panel'] = panel
        panel.update_state(self.state)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def apply_rate(self, window, rate: float):
        """Make Electrum's next send dialog open at this rate (sub-1 included)."""
        sat_per_kvb = int(round(rate * 1000))
        self.config.FEE_POLICY = f'feerate:{sat_per_kvb}'
        try:
            window.show_send_tab()
        except Exception:
            pass
        for row in self._send_rows:
            if row.window() is window:
                row.label.setText(f"LowTide: {_('next send will use')} <b>{fmt_rate(rate)} sat/vB</b>")
        try:
            window.show_tooltip_after_delay(_('Fee rate set to {} sat/vB for the next send. Enter the payment and press Pay.').format(fmt_rate(rate)))
        except Exception:
            pass

    def notify_all(self, message: str, *, title: str = 'LowTide', actions: Optional[List[tuple]] = None):
        """Desktop notification + in-app cue, once per wallet window."""
        for window in list(self._windows):
            try:
                qt_notify.notify(window, message, title=title, use_osascript=bool(self.config.LOWTIDE_OSASCRIPT_NOTIFY))
                if actions is not None:
                    d = LowTideAlert(self, window, title, message, actions)
                    d.show()
                    self._alerts.append(d)
            except Exception:
                self.logger.exception('notify failed')

    def _maybe_notify_low_tide(self, st: State):
        if not self.notifications_enabled():
            return
        cur = st.current_window()
        if cur is None:
            return
        key = f'lowtide:{cur.start}'
        if key in self._notified:
            return
        self._notified.add(key)
        self._save_notified()
        msg = _('Low tide has started: ≈{} sat/vB until {}. A good time for consolidation and queued payments.').format(
            fmt_rate(cur.rate), fmt_time(cur.end))
        actions = [(_('Open forecast'), lambda: self.open_panel(next(iter(self._windows), None)))]
        self.notify_all(msg, actions=actions)

    def _save_notified(self):
        try:
            import os
            path = os.path.join(self.data_dir, 'notified.json')
            with open(path, 'w') as f:
                json.dump(sorted(self._notified), f)
        except Exception:
            pass

    def _load_notified(self):
        try:
            import os
            with open(os.path.join(self.data_dir, 'notified.json')) as f:
                self._notified = set(json.load(f))
        except Exception:
            self._notified = set()

    # --- settings -----------------------------------------------------

    def requires_settings(self) -> bool:
        return True

    def settings_widget(self, window):
        b = QPushButton(_('Settings'))
        b.clicked.connect(lambda: self.settings_dialog(window))
        return b

    def settings_dialog(self, window):
        d = WindowModalDialog(window, 'LowTide ' + _('Settings'))
        vbox = QVBoxLayout(d)
        vbox.addWidget(WWLabel(_('The forecast uses only aggregate network data. Nothing about your wallet leaves this machine. '
                                 'A txid is sent to mempool.space only when you open the accelerator option.')))
        grid = QGridLayout()
        grid.addWidget(QLabel(_('mempool instance URL')), 0, 0)
        url_e = QLineEdit(self.config.LOWTIDE_MEMPOOL_URL)
        url_e.setPlaceholderText('https://mempool.space  or  http://umbrel.local:3006')
        url_e.setMinimumWidth(340)
        grid.addWidget(url_e, 0, 1)
        grid.addWidget(QLabel(_('Refresh every (minutes)')), 1, 0)
        poll_e = QLineEdit(str(self.config.LOWTIDE_POLL_MINUTES))
        poll_e.setFixedWidth(60)
        grid.addWidget(poll_e, 1, 1)
        notif_cb = QCheckBox(_('Desktop notification when low tide starts'))
        notif_cb.setChecked(bool(self.config.LOWTIDE_NOTIFICATIONS))
        grid.addWidget(notif_cb, 2, 0, 1, 2)
        osa_cb = QCheckBox(_('Also use the macOS notification center (fallback if tray banners do not show)'))
        osa_cb.setChecked(bool(self.config.LOWTIDE_OSASCRIPT_NOTIFY))
        grid.addWidget(osa_cb, 3, 0, 1, 2)
        privacy_cb = QCheckBox(_('Privacy mode: live mempool data from the Electrum server only (history still needs a mempool instance)'))
        privacy_cb.setChecked(bool(self.config.LOWTIDE_PRIVACY_MODE))
        grid.addWidget(privacy_cb, 4, 0, 1, 2)
        demo_cb = QCheckBox(_('Demo mode (developer switches)'))
        demo_cb.setChecked(bool(self.config.LOWTIDE_DEMO_MODE))
        grid.addWidget(demo_cb, 5, 0, 1, 2)
        vbox.addLayout(grid)
        test_hbox = QHBoxLayout()
        test_btn = QPushButton(_('Send test notification'))
        def do_test():
            main_window = self._main_window_for(window)
            if main_window is None:
                d.show_error(_('Open a wallet window first.'))
                return
            qt_notify.notify(main_window, _('LowTide test: low tide starts now ≈0.5 sat/vB'), use_osascript=osa_cb.isChecked())
            a = LowTideAlert(self, main_window, _('Test'), _('This is the in-app cue that accompanies every desktop notification.'), [])
            a.show()
            self._alerts.append(a)
        test_btn.clicked.connect(do_test)
        test_hbox.addWidget(test_btn)
        test_hbox.addStretch(1)
        vbox.addLayout(test_hbox)
        vbox.addWidget(WWLabel(_('Alerts only work while Electrum is running.')))
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if not d.exec():
            return
        old_url = self.config.LOWTIDE_MEMPOOL_URL
        self.config.LOWTIDE_MEMPOOL_URL = url_e.text().strip() or 'https://mempool.space'
        try:
            self.config.LOWTIDE_POLL_MINUTES = max(5, int(poll_e.text()))
        except ValueError:
            pass
        self.config.LOWTIDE_NOTIFICATIONS = notif_cb.isChecked()
        self.config.LOWTIDE_OSASCRIPT_NOTIFY = osa_cb.isChecked()
        self.config.LOWTIDE_PRIVACY_MODE = privacy_cb.isChecked()
        self.config.LOWTIDE_DEMO_MODE = demo_cb.isChecked()
        if old_url != self.config.LOWTIDE_MEMPOOL_URL:
            self.service.refresh_now()

    def _main_window_for(self, window) -> Optional['ElectrumWindow']:
        if window in self._windows:
            return window
        for w in self._windows:
            return w
        return None
