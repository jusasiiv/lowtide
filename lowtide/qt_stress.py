"""Wallet stress test and tech check dialog."""
import math
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QSlider,
                             QPushButton, QHeaderView, QAbstractItemView)

from electrum.i18n import _
from electrum.gui.qt.util import WindowModalDialog, Buttons, CloseButton, WWLabel

from .core import stress as S
from .core.mempool_api import SPIKE_FALLBACK
from .fmt import fmt_rate, fmt_sats_fiat
from .qt_wallet import wallet_coins, wallet_script_info

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow


def slider_to_rate(pos: int) -> float:
    """0..1000 → 0.1 .. 2000 sat/vB, log scale."""
    return round(10 ** (-1 + pos / 1000.0 * (math.log10(2000) + 1)), 1)


def rate_to_slider(rate: float) -> int:
    return int(round((math.log10(max(0.1, rate)) + 1) / (math.log10(2000) + 1) * 1000))


class StressDialog(WindowModalDialog):

    def __init__(self, plugin, window: 'ElectrumWindow'):
        WindowModalDialog.__init__(self, window, 'LowTide — ' + _('Wallet stress test'))
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.setMinimumWidth(780)
        st = plugin.state
        coins, _map = wallet_coins(self.wallet)
        self.coins = [c for c in coins if not c.frozen]
        script_type, m, n = wallet_script_info(self.wallet)
        self.tech = S.tech_check(script_type, m, n)
        self.output_type = 'p2wpkh' if script_type in ('p2wpkh', 'p2wpkh-p2sh', 'p2pkh') else 'p2wsh'

        vbox = QVBoxLayout(self)
        n_unconf = sum(1 for c in self.coins if not c.confirmed)
        head = QLabel(f"<b>{len(self.coins)} {_('coins')}</b>, {fmt_sats_fiat(window, sum(c.value for c in self.coins))}"
                      + (f" ({n_unconf} {_('unconfirmed')})" if n_unconf else ''))
        vbox.addWidget(head)

        tech = WWLabel(f"<b>{_('Tech check')}: {self.tech.label}</b> — ~{self.tech.input_vb:.0f} vB {_('per input')}. {self.tech.message}")
        tech.setStyleSheet('color: #1e8f3e' if self.tech.ok else 'color: #b06000')
        vbox.addWidget(tech)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([_('Scenario'), 'sat/vB', _('Fee to spend every coin'), _('Fiat'), _('% of balance'), _('Uneconomical coins')])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        vbox.addWidget(self.table)

        hb = QHBoxLayout()
        hb.addWidget(QLabel(_('Custom rate')))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(rate_to_slider(100.0))
        self.slider.valueChanged.connect(self.refresh)
        hb.addWidget(self.slider, 1)
        self.slider_label = QLabel('')
        hb.addWidget(self.slider_label)
        vbox.addLayout(hb)

        self.summary = WWLabel('')
        vbox.addWidget(self.summary)
        note = WWLabel(_('Spike levels are the highest block-median fee rates of those years on mempool.space. '
                         'Taproot is not offered because Electrum cannot create Taproot wallets.'))
        note.setStyleSheet('color: gray')
        vbox.addWidget(note)

        buttons = []
        self.consolidate_btn = QPushButton(_('Consolidate…'))
        self.consolidate_btn.clicked.connect(lambda: (self.close(), plugin.open_consolidate(window)))
        buttons.append(self.consolidate_btn)
        if not self.tech.ok:
            self.migrate_btn = QPushButton(_('Migrate to native segwit…'))
            self.migrate_btn.clicked.connect(lambda: (self.close(), plugin.open_consolidate(window, migration=True)))
            buttons.append(self.migrate_btn)
        vbox.addLayout(Buttons(*buttons, CloseButton(self)))
        self.refresh()

    def scenarios(self) -> List[S.Scenario]:
        st = self.plugin.state
        low = None
        if st.forecast:
            nxt = st.current_window() or st.next_window()
            low = nxt.rate if nxt else min(p.median for p in st.forecast.horizon)
        if st.pile and (low is None or st.pile.rate < low):
            low = st.pile.rate
        custom = slider_to_rate(self.slider.value())
        self.slider_label.setText(f'{fmt_rate(custom)} sat/vB')
        return S.default_scenarios(st.next_block_rate, low, st.spikes or SPIKE_FALLBACK, custom_rate=custom)

    def refresh(self):
        rows = S.stress_table(self.coins, self.scenarios(), output_type=self.output_type)
        self.table.setRowCount(len(rows))
        worst = None
        for i, r in enumerate(rows):
            vals = [r.scenario.name, fmt_rate(r.scenario.rate), f'{r.fee:,} sats',
                    self._fiat(r.fee), f'{r.fee_share * 100:.1f}%' if r.balance else '—',
                    f'{len(r.uneconomical)} ({r.uneconomical_value:,} sats)' if r.uneconomical else '0']
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j > 0:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if r.scenario.kind == 'lowtide':
                    it.setForeground(Qt.GlobalColor.darkGreen)
                elif r.scenario.kind == 'spike' and r.uneconomical:
                    it.setForeground(Qt.GlobalColor.darkRed)
                self.table.setItem(i, j, it)
            if r.scenario.kind == 'spike' and (worst is None or r.fee > worst.fee):
                worst = r
        if rows and worst:
            low = next((r for r in rows if r.scenario.kind == 'lowtide'), None)
            txt = (f"{_('Spending everything costs')} <b>{rows[0].fee:,} sats</b> {_('now')}"
                   + (f", <b>{low.fee:,} sats</b> {_('at low tide')}" if low else '')
                   + f" {_('and')} <b>{worst.fee:,} sats</b> {_('at the')} {worst.scenario.name}.")
            if worst.uneconomical:
                txt += f" {_('At that level')} {len(worst.uneconomical)} {_('of your coins')} ({worst.uneconomical_value:,} sats) {_('cost more to spend than they are worth.')}"
            self.summary.setText(txt)
        elif not rows:
            self.summary.setText(_('No coins in this wallet.'))

    def _fiat(self, sats: int) -> str:
        try:
            fx = self.window.fx
            return fx.format_amount_and_units(sats) if fx and fx.is_enabled() else '—'
        except Exception:
            return '—'
