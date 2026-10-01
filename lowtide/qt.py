"""Qt entry point for LowTide."""
from functools import partial
from typing import TYPE_CHECKING, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QToolButton, QStatusBar, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QCheckBox, QPushButton, QGridLayout)

from electrum.i18n import _
from electrum.plugin import hook
from electrum.gui.qt.util import (WindowModalDialog, Buttons, CloseButton, OkButton, CancelButton,
                                  read_QIcon_from_bytes, WWLabel)

from .lowtide import LowTidePlugin
from . import qt_notify
from .core.version import CORE_VERSION

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


class StatusButton(QToolButton):
    """Flat text button in the status bar: the always-visible tide indicator."""

    def __init__(self):
        QToolButton.__init__(self)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_state(text='LowTide', tooltip=_('Fee tide forecast: loading…'))

    def set_state(self, *, text: str, tooltip: str, color: Optional[str] = None):
        self.setText('⛵ ' + text)
        self.setToolTip(tooltip)
        self.setStyleSheet(f'QToolButton {{ color: {color}; font-weight: bold; }}' if color else '')


class Plugin(LowTidePlugin):

    def __init__(self, parent, config, name):
        LowTidePlugin.__init__(self, parent, config, name)
        self._status_buttons = {}  # type: Dict[int, StatusButton]  # id(QStatusBar) -> button
        self._windows = {}  # type: Dict[ElectrumWindow, dict]
        self._icon_bytes = self.read_file('lowtide.png')
        self.logger.info(f'LowTide core {CORE_VERSION}')

    def icon(self):
        return read_QIcon_from_bytes(self._icon_bytes)

    # --- hooks --------------------------------------------------------

    @hook
    def create_status_bar(self, sb: QStatusBar):
        btn = StatusButton()
        btn.clicked.connect(partial(self._on_status_clicked, btn))
        sb.addWidget(btn)
        self._status_buttons[id(sb)] = btn

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
        self._windows[window] = {'wallet': wallet}
        btn = self._status_buttons.get(id(window.statusBar()))
        if btn:
            self._windows[window]['status_button'] = btn

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet'):
        for window in [w for w, st in self._windows.items() if st.get('wallet') is wallet]:
            self._windows.pop(window, None)

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        self._windows.pop(window, None)
        self._status_buttons.pop(id(window.statusBar()), None)

    # --- actions ------------------------------------------------------

    def _on_status_clicked(self, btn: StatusButton):
        window = btn.window()
        self.open_panel(window)

    def open_panel(self, window):
        # M0 placeholder; the forecast panel arrives in M1.
        window.show_message(_('LowTide forecast panel: coming in M1.'), title='LowTide')

    def requires_settings(self) -> bool:
        return True

    def settings_widget(self, window):
        return EnterButtonCompat(_('Settings'), partial(self.settings_dialog, window))

    def settings_dialog(self, window):
        d = WindowModalDialog(window, 'LowTide ' + _('Settings'))
        vbox = QVBoxLayout(d)
        vbox.addWidget(WWLabel(_('The forecast uses only aggregate network data. Nothing about your wallet leaves this machine. '
                                 'A txid is sent to mempool.space only when you open the accelerator option.')))
        grid = QGridLayout()
        grid.addWidget(QLabel(_('mempool instance URL')), 0, 0)
        url_e = QLineEdit(self.config.LOWTIDE_MEMPOOL_URL)
        url_e.setPlaceholderText('https://mempool.space  or  http://umbrel.local:3006')
        url_e.setMinimumWidth(320)
        grid.addWidget(url_e, 0, 1)
        notif_cb = QCheckBox(_('Desktop notification when low tide starts'))
        notif_cb.setChecked(bool(self.config.LOWTIDE_NOTIFICATIONS))
        grid.addWidget(notif_cb, 1, 0, 1, 2)
        osa_cb = QCheckBox(_('Also use macOS "display notification" (fallback if tray banners do not show)'))
        osa_cb.setChecked(bool(self.config.LOWTIDE_OSASCRIPT_NOTIFY))
        grid.addWidget(osa_cb, 2, 0, 1, 2)
        privacy_cb = QCheckBox(_('Privacy mode: live data from the Electrum server only'))
        privacy_cb.setChecked(bool(self.config.LOWTIDE_PRIVACY_MODE))
        grid.addWidget(privacy_cb, 3, 0, 1, 2)
        demo_cb = QCheckBox(_('Demo mode (developer switches)'))
        demo_cb.setChecked(bool(self.config.LOWTIDE_DEMO_MODE))
        grid.addWidget(demo_cb, 4, 0, 1, 2)
        vbox.addLayout(grid)

        test_hbox = QHBoxLayout()
        test_btn = QPushButton(_('Send test notification'))
        def do_test():
            main_window = self._main_window_for(window)
            if main_window is None:
                d.show_error(_('Open a wallet window first.'))
                return
            qt_notify.notify(main_window, _('LowTide test: low tide starts now ≈0.5 sat/vB'),
                             use_osascript=osa_cb.isChecked())
        test_btn.clicked.connect(do_test)
        test_hbox.addWidget(test_btn)
        test_hbox.addStretch(1)
        vbox.addLayout(test_hbox)
        vbox.addWidget(WWLabel(_('Alerts only work while Electrum is running.')))
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if not d.exec():
            return
        self.config.LOWTIDE_MEMPOOL_URL = url_e.text().strip() or 'https://mempool.space'
        self.config.LOWTIDE_NOTIFICATIONS = notif_cb.isChecked()
        self.config.LOWTIDE_OSASCRIPT_NOTIFY = osa_cb.isChecked()
        self.config.LOWTIDE_PRIVACY_MODE = privacy_cb.isChecked()
        self.config.LOWTIDE_DEMO_MODE = demo_cb.isChecked()

    def _main_window_for(self, window) -> Optional['ElectrumWindow']:
        if window in self._windows:
            return window
        for w in self._windows:
            return w
        return None


class EnterButtonCompat(QPushButton):
    def __init__(self, text, func):
        QPushButton.__init__(self, text)
        self.clicked.connect(lambda: func())
