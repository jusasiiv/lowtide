"""Qt entry point for LowTide."""
import json
import os
import time
from functools import partial
from typing import TYPE_CHECKING, Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QToolButton, QStatusBar, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QCheckBox, QPushButton, QGridLayout, QDialog, QWidget)

from electrum.i18n import _
from electrum.network import Network
from electrum.plugin import hook
from electrum.transaction import Transaction
from electrum.gui.qt.util import (WindowModalDialog, Buttons, CloseButton, OkButton, CancelButton,
                                  read_QIcon_from_bytes, WWLabel)

from .lowtide import LowTidePlugin
from .qt_notify import notify as desktop_notify
from .core.version import CORE_VERSION
from .core.store import Store
from .core import eta as E
from .core import accelerator as A
from .core import rescue as R
from .service import LowTideService, State
from .fmt import fmt_rate, fmt_time, fmt_window, fmt_duration
from .qt_panel import ForecastPanel
from .qt_stress import StressDialog
from .qt_consolidate import ConsolidateDialog
from .qt_rescue import RescueDialog

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.gui.qt.transaction_dialog import TxDialog
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
            self.label.setText(f"LowTide: <b>{fmt_rate(st.pile.rate)} sat/vB</b> {_('jumps the pile')} · {self.plugin.fmt_eta(e, short=True)}")
            self.label.setToolTip(self.plugin.fmt_eta(e, verbose=True).replace('<br>', '\n'))
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
            b.clicked.connect(lambda _checked=False, fn=fn: (self.close(), fn()))
            hb.addWidget(b)
        hb.addStretch(1)
        hb.addWidget(CloseButton(self))
        v.addLayout(hb)


class Plugin(LowTidePlugin):

    def __init__(self, parent, config, name):
        LowTidePlugin.__init__(self, parent, config, name)
        self._status_buttons = {}  # type: Dict[int, StatusButton]
        self._send_rows = []       # type: List[SendTabRow]
        self._windows = {}         # type: Dict[ElectrumWindow, dict]
        self._rescue_buttons = {}  # type: Dict[int, QPushButton]  # id(TxDialog) -> button
        self._icon_bytes = self.read_file('lowtide.png')
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
        self._monitor = QTimer()
        self._monitor.setInterval(60_000)
        self._monitor.timeout.connect(self._monitor_tick)
        self._monitor.start()

    def on_close(self):
        self._monitor.stop()
        self.service.stop()

    def icon(self):
        return read_QIcon_from_bytes(self._icon_bytes)

    # --- transport ----------------------------------------------------

    def network_available(self) -> bool:
        return Network.get_instance() is not None

    def _http_get(self, url: str, timeout: float = 20.0):
        if Network.get_instance() is None:
            raise Exception('offline: no network')
        text = Network.send_http_on_proxy('get', url, timeout=timeout)
        return json.loads(text) if text and text.strip() else None

    def _http_post(self, url: str, body: dict, timeout: float = 30.0):
        if Network.get_instance() is None:
            raise Exception('offline: no network')
        text = Network.send_http_on_proxy('post', url, json=body, timeout=timeout)
        return json.loads(text) if text and text.strip() else None

    def _base_url(self) -> str:
        network = Network.get_instance()
        is_tor = bool(getattr(network, 'is_proxy_tor', False)) if network else False
        return self.mempool_base_url(is_tor=is_tor)

    def accelerator_client(self) -> A.AcceleratorClient:
        network = Network.get_instance()
        is_tor = bool(getattr(network, 'is_proxy_tor', False)) if network else False
        return A.AcceleratorClient(self.accelerator_base_url(is_tor=is_tor), lambda u: self._http_get(u), lambda u, b: self._http_post(u, b))

    def _server_histogram(self):
        network = Network.get_instance()
        if network is None:
            return None
        data = getattr(network.mempool_fees, '_data', None)
        return list(data) if data else None

    def relay_rate(self) -> float:
        network = Network.get_instance()
        if network is None or network.relay_fee is None:
            return 1.0
        return max(0.1, network.relay_fee / 1000.0)

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
        m.addAction(_('Wallet stress test'), lambda: self.open_stress(window))
        m.addAction(_('Consolidate at low tide…'), lambda: self.open_consolidate(window))
        m.addSeparator()
        m.addAction(_('Settings'), lambda: self.settings_dialog(window))

    @hook
    def qt_utxo_menu(self, menu, coins, wallet):
        window = self._window_for_wallet(wallet)
        if window is None or not coins:
            return
        outpoints = [c.prevout.to_str() for c in coins]
        menu.addSeparator()
        menu.addAction(_('Consolidate selected at low tide…'), lambda: self.open_consolidate(window, preselected=outpoints))

    @hook
    def transaction_dialog(self, d: 'TxDialog'):
        btn = QPushButton('⛵ ' + _('Rescue…'))
        btn.setToolTip(_('Compare Wait, RBF, CPFP and the mempool accelerator for this unconfirmed transaction.'))
        btn.clicked.connect(partial(self._rescue_from_dialog, d))
        btn.hide()
        d.buttons.insert(0, btn)
        self._rescue_buttons[id(d)] = btn

    @hook
    def transaction_dialog_update(self, d: 'TxDialog'):
        btn = self._rescue_buttons.get(id(d))
        if btn is None:
            return
        show = False
        try:
            tx = d.tx
            if tx is not None and tx.is_complete() and tx.txid():
                info = d.wallet.get_tx_info(tx)
                mined = info.tx_mined_status
                show = mined.height() <= 0 and not mined.is_local_like() and d.wallet.db.get_transaction(tx.txid()) is not None
        except Exception:
            show = False
        btn.setVisible(show)

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        if window is None:
            return
        self._windows[window] = {'wallet': wallet, 'panel': None, 'stranded': []}
        QTimer.singleShot(5000, self._monitor_tick)

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
        wst = self._windows.get(window, {})
        relay_warn = self.relay_warning_text()
        cur, nxt = st.current_window(), st.next_window()
        text = f"{fmt_rate(st.next_block_rate)} sat/vB"
        tip = [f"{_('Next block')}: {fmt_rate(st.next_block_rate)} sat/vB"]
        color, bold = None, False
        if cur:
            text += f" · {_('low tide now')} {_('until')} {fmt_time(cur.end)}"
            tip.append(_('Low tide: a good time for consolidation and non-urgent sends.') + f" ≈{fmt_rate(cur.rate)} sat/vB")
            color, bold = '#1e8f3e', True
        elif nxt:
            text += f" · {_('low tide')} {fmt_window(nxt)}"
            tip.append(f"{_('Next low tide')} {_('in')} {fmt_duration(nxt.start - st.now())}, ≈{fmt_rate(nxt.rate)} sat/vB")
        if st.pile:
            tip.append(f"{_('Pile-jump rate')}: {fmt_rate(st.pile.rate)} sat/vB")
        stranded = wst.get('stranded') or []
        if stranded:
            text = f"⚠ {len(stranded)} {_('stranded')} · " + text
            tip.append(_('{} unconfirmed transaction(s) waited longer than the forecast expected. Open them for rescue options.').format(len(stranded)))
            color, bold = '#b06000', True
        if relay_warn:
            text += ' · ' + _('server blocks sub-1')
            tip.append(relay_warn)
            color = '#b06000'
        if st.offline:
            tip.append(_('Offline: showing cached data as of') + ' ' + (fmt_time(st.data_as_of) if st.data_as_of else '—'))
        tip.append(_('Click for the tide forecast.'))
        btn.set_state(text=text, tooltip='\n'.join(tip), color=color, bold=bold)

    def relay_warning_text(self) -> Optional[str]:
        """Warn when the connected Electrum server refuses fees below 1 sat/vB."""
        network = Network.get_instance()
        if network is None or network.relay_fee is None:
            return None
        if network.relay_fee <= RELAY_SUB1_SAT_PER_KVB:
            return None
        return (_('Your Electrum server only relays fees from {} sat/vB, so Electrum will block sub-1 sat/vB sends ("below relay fee"). ')
                .format(fmt_rate(network.relay_fee / 1000))
                + _('Switch to a server that relays 0.1 sat/vB: Tools ▸ Network, untick "Select server automatically" and pick another one; most default servers relay down to 0.1.'))

    def fmt_eta(self, e: Optional[E.Eta], verbose: bool = False, short: bool = False) -> str:
        if e is None:
            return _('ETA unknown')
        now = time.time()
        if e.median_s is None:
            return _('not before the tide turns')
        when = fmt_time(now + e.median_s)
        if short:
            return f"{_('likely by')} {when}"
        txt = f"{_('likely by')} {when} ({fmt_duration(e.median_s)})"
        if e.p90_s is not None and e.p90_s > e.median_s * 1.3 + 600:
            txt += f", {_('90% by')} {fmt_time(now + e.p90_s)}"
        if e.note == 'when the tide turns':
            txt += ' · ' + _('when the tide turns')
        if verbose:
            txt += f"<br>{_('Queue ahead')}: {e.queue_vb / 1e6:.2f} vMB · {_('arriving above this rate')}: {e.arrival_vb_per_s:.0f} vB/s"
            if e.blocks:
                txt += f" · {e.blocks} {_('blocks')}"
        return txt

    # --- stranded / sub-1 safety net ----------------------------------

    def first_seen(self, wallet, txid: str) -> Optional[float]:
        rec = (self.get_storage(wallet).get('tx_watch') or {}).get(txid)
        return rec.get('first_seen') if rec else None

    def _monitor_tick(self):
        st = self.state
        for window, wst in list(self._windows.items()):
            wallet = wst.get('wallet')
            if wallet is None:
                continue
            try:
                self._watch_wallet(window, wallet, wst, st)
            except Exception:
                self.logger.exception('monitor failed')

    def _watch_wallet(self, window, wallet, wst, st: State):
        now = time.time()
        storage = self.get_storage(wallet)
        watch = storage.setdefault('tx_watch', {})
        try:
            unconf = dict(wallet.adb.unconfirmed_tx)
        except Exception:
            unconf = {}
        stranded = []
        changed = False
        for txid in list(unconf):
            tx = wallet.db.get_transaction(txid)
            if tx is None:
                continue
            fee = wallet.adb.get_tx_fee(txid)
            vsize = tx.estimated_size()
            rate = (fee / vsize) if (fee and vsize) else None
            rec = watch.get(txid)
            if rec is None:
                e = st.eta(rate) if (rate and st.hist) else None
                rec = {'first_seen': int(now), 'rate': rate, 'p90_s': (e.p90_s if e else None), 'median_s': (e.median_s if e else None)}
                watch[txid] = rec
                changed = True
            age = now - rec['first_seen']
            reasons = []
            expected = rec.get('p90_s')
            if expected is not None and age > expected and age > 1800:
                reasons.append('stranded')
            elif expected is None and age > 24 * 3600:
                reasons.append('stranded')
            if rate is not None and st.hist and rate < st.mempool_min_fee - 1e-9:
                reasons.append('evicting')
            if age > 12 * 86400:
                reasons.append('expiring')
            if reasons:
                stranded.append(txid)
                key = f'stranded:{txid}:{reasons[0]}'
                if key not in self._notified and self.notifications_enabled():
                    self._notified.add(key)
                    self._save_notified()
                    label = wallet.get_label_for_txid(txid) or txid[:16] + '…'
                    if 'evicting' in reasons:
                        msg = _('"{}" pays {} sat/vB, below the mempool minimum of {} sat/vB: nodes are dropping it. Bump or rebroadcast it.').format(
                            label, fmt_rate(rate), fmt_rate(st.mempool_min_fee))
                    elif 'expiring' in reasons:
                        msg = _('"{}" is close to the 2-week mempool expiry. Bump or rebroadcast it.').format(label)
                    else:
                        msg = _('"{}" has waited longer than the forecast expected ({}). Open it to compare rescue options.').format(label, fmt_duration(age))
                    self.notify_all(msg, title=_('Stranded transaction'),
                                    actions=[(_('Rescue…'), partial(self.open_rescue, window, tx))], only_window=window)
        # forget confirmed ones
        for txid in list(watch):
            if txid not in unconf:
                watch.pop(txid, None)
                changed = True
        if changed:
            try:
                wallet.save_db()
            except Exception:
                pass
        if stranded != wst.get('stranded'):
            wst['stranded'] = stranded
            btn = self._status_buttons.get(id(window.statusBar()))
            if btn:
                self._update_status_button(btn, st)
            panel = wst.get('panel')
            if panel is not None and panel.isVisible():
                panel.update_state(st)

    def stranded_txs(self, window) -> List[str]:
        return list((self._windows.get(window) or {}).get('stranded') or [])

    def record_acceleration(self, wallet, txid: str, fields: dict):
        storage = self.get_storage(wallet)
        acc = storage.setdefault('accelerations', {})
        rec = acc.setdefault(txid, {})
        rec.update(fields)
        rec['updated'] = int(time.time())
        try:
            wallet.save_db()
        except Exception:
            pass

    # --- actions ------------------------------------------------------

    def _window_for_wallet(self, wallet):
        for w, st in self._windows.items():
            if st.get('wallet') is wallet:
                return w
        return None

    def _on_status_clicked(self, btn: StatusButton):
        self.open_panel(btn.window())

    def open_panel(self, window):
        wst = self._windows.get(window)
        if wst is None:
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

    def open_stress(self, window):
        StressDialog(self, window).exec()

    def open_consolidate(self, window, *, preselected=None, migration=False):
        ConsolidateDialog(self, window, preselected=preselected, migration=migration).exec()

    def open_rescue(self, window, tx: Transaction):
        RescueDialog(self, window, tx).exec()

    def open_rescue_txid(self, window, txid: str):
        tx = window.wallet.db.get_transaction(txid)
        if tx is not None:
            self.open_rescue(window, tx)

    def _rescue_from_dialog(self, d: 'TxDialog'):
        window = d.main_window
        self.open_rescue(window, d.tx)

    def on_queue_changed(self, window):
        pass  # M4

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

    def notify_all(self, message: str, *, title: str = 'LowTide', actions: Optional[List[tuple]] = None, only_window=None):
        """Desktop notification + in-app cue."""
        windows = [only_window] if only_window is not None else list(self._windows)
        for window in windows:
            try:
                desktop_notify(window, message, title=title, use_osascript=bool(self.config.LOWTIDE_OSASCRIPT_NOTIFY))
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
        for w in list(self._windows):
            actions.append((_('Consolidate…'), partial(self.open_consolidate, w)))
            break
        self.notify_all(msg, title=_('Low tide'), actions=actions)

    def _save_notified(self):
        try:
            with open(os.path.join(self.data_dir, 'notified.json'), 'w') as f:
                json.dump(sorted(self._notified), f)
        except Exception:
            pass

    def _load_notified(self):
        try:
            with open(os.path.join(self.data_dir, 'notified.json')) as f:
                self._notified = set(json.load(f))
        except Exception:
            self._notified = set()

    def demo_reset(self):
        """Clear everything a demo run leaves behind so the next judge group starts fresh."""
        self._notified = set()
        self._save_notified()
        self.state.demo = {}
        for w, wst in self._windows.items():
            wst['stranded'] = []
            storage = self.get_storage(wst['wallet'])
            storage.pop('tx_watch', None)
            try:
                wst['wallet'].save_db()
            except Exception:
                pass
        self._on_state(self.state)

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
        notif_cb = QCheckBox(_('Desktop notifications (low tide, stranded transactions, due payments)'))
        notif_cb.setChecked(bool(self.config.LOWTIDE_NOTIFICATIONS))
        grid.addWidget(notif_cb, 2, 0, 1, 2)
        osa_cb = QCheckBox(_('Also use the macOS notification center'))
        osa_cb.setChecked(bool(self.config.LOWTIDE_OSASCRIPT_NOTIFY))
        grid.addWidget(osa_cb, 3, 0, 1, 2)
        privacy_cb = QCheckBox(_('Privacy mode: live mempool data from the Electrum server only (history still needs a mempool instance)'))
        privacy_cb.setChecked(bool(self.config.LOWTIDE_PRIVACY_MODE))
        grid.addWidget(privacy_cb, 4, 0, 1, 2)
        demo_cb = QCheckBox(_('Demo mode (developer switches in the forecast panel)'))
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
            desktop_notify(main_window, _('LowTide test: low tide starts now ≈0.5 sat/vB'), use_osascript=osa_cb.isChecked())
            a = LowTideAlert(self, main_window, _('Test'), _('This is the in-app cue that accompanies every desktop notification.'), [])
            a.show()
            self._alerts.append(a)
        test_btn.clicked.connect(do_test)
        test_hbox.addWidget(test_btn)
        reset_btn = QPushButton(_('Demo reset'))
        reset_btn.setToolTip(_('Clears alerts, stranded flags and demo switches.'))
        reset_btn.clicked.connect(self.demo_reset)
        test_hbox.addWidget(reset_btn)
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
        self._on_state(self.state)

    def _main_window_for(self, window) -> Optional['ElectrumWindow']:
        if window in self._windows:
            return window
        for w in self._windows:
            return w
        return None
