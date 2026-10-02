"""Privacy-aware consolidation and migration dialog."""
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QRadioButton,
                             QButtonGroup, QLineEdit, QCheckBox, QPushButton, QGridLayout)

from electrum.i18n import _
from electrum.bitcoin import is_address
from electrum.fee_policy import FeePolicy
from electrum.transaction import PartialTxOutput
from electrum.gui.qt.util import WindowModalDialog, Buttons, CancelButton, WWLabel

from .core import consolidate as C
from .core import histogram as H
from .fmt import fmt_rate, fmt_sats_fiat
from .qt_wallet import wallet_coins, wallet_script_info

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow


class ConsolidateDialog(WindowModalDialog):

    def __init__(self, plugin, window: 'ElectrumWindow', *, preselected: Optional[Sequence[str]] = None, migration: bool = False):
        title = _('Migrate to native segwit') if migration else _('Consolidate at low tide')
        WindowModalDialog.__init__(self, window, 'LowTide — ' + title)
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.migration = migration
        self.setMinimumWidth(760)
        coins, self.by_outpoint = wallet_coins(self.wallet)
        if preselected:
            sel = set(preselected)
            coins = [c for c in coins if c.outpoint in sel]
        self.groups = C.group_coins(coins)
        skipped = [c for c in coins if c.frozen or not c.confirmed]
        script_type, _m, _n = wallet_script_info(self.wallet)
        self.output_type = 'p2wpkh' if migration or script_type in ('p2wpkh', 'p2wpkh-p2sh', 'p2pkh') else 'p2wsh'

        vbox = QVBoxLayout(self)
        intro = (_('Create a new native segwit wallet in Electrum (File ▸ New/Restore, default type), copy one of its receiving addresses here, '
                   'and LowTide prepares the migration as a low-tide consolidation. Nothing is sent until you sign in the preview.')
                 if migration else
                 _('Coins with different labels are kept apart, so consolidating does not link what you kept separate. '
                   'Consolidation is never urgent: the default fee is the floor of the mempool.'))
        vbox.addWidget(WWLabel(intro))
        if skipped:
            vbox.addWidget(WWLabel(_('{} coins are skipped because they are frozen or unconfirmed.').format(len(skipped))))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([_('Group / coin'), _('Coins'), _('Value'), _('Sources')])
        self.tree.setColumnWidth(0, 340)
        for g in self.groups:
            it = QTreeWidgetItem([g.title, str(len(g.coins)), f'{g.value:,} sats', str(g.sources)])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if (len(g.coins) >= 2 or preselected) else Qt.CheckState.Unchecked)
            it.setData(0, Qt.ItemDataRole.UserRole, g.key)
            for c in g.coins:
                ch = QTreeWidgetItem([f'{c.outpoint[:10]}…{c.outpoint[-4:]}', '', f'{c.value:,} sats', f'~{c.vsize:.0f} vB'])
                ch.setFlags(ch.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                it.addChild(ch)
            self.tree.addTopLevelItem(it)
        self.tree.itemChanged.connect(lambda *_: self.refresh())
        vbox.addWidget(self.tree)

        self.merge_cb = QCheckBox(_('Merge the selected groups into one transaction (this publicly links them)'))
        self.merge_cb.setChecked(bool(preselected) and len(self.groups) > 1)
        self.merge_cb.toggled.connect(self.refresh)
        vbox.addWidget(self.merge_cb)

        if migration:
            hb = QHBoxLayout()
            hb.addWidget(QLabel(_('Destination address (new native segwit wallet)')))
            self.dest_e = QLineEdit()
            self.dest_e.setPlaceholderText('bc1q…')
            self.dest_e.textChanged.connect(self.refresh)
            hb.addWidget(self.dest_e, 1)
            vbox.addLayout(hb)
        else:
            self.dest_e = None

        grid = QGridLayout()
        grid.addWidget(QLabel(_('Fee rate')), 0, 0)
        self.rate_group = QButtonGroup(self)
        st = plugin.state
        self.rate_options = []
        floor = max(H.FLOOR_RATE, H.quantize(st.mempool_min_fee + 0.1)) if st.hist else 0.2
        self.rate_options.append((_('Floor'), floor, _('not urgent: the bottom of the mempool')))
        if st.pile:
            self.rate_options.append((_('Pile-jump'), st.pile.rate, _('ahead of the floor pile')))
        if st.forecast:
            w = st.current_window() or st.next_window()
            if w:
                self.rate_options.append((_('Low tide'), w.rate, _('forecast for the next low tide')))
        self.rate_options.append((_('Custom'), None, ''))
        self.custom_e = QLineEdit('1.0')
        self.custom_e.setFixedWidth(60)
        self.custom_e.textChanged.connect(self.refresh)
        col = 1
        for i, (name, rate, hint) in enumerate(self.rate_options):
            rb = QRadioButton(f'{name} ({fmt_rate(rate)} sat/vB)' if rate is not None else name)
            rb.setToolTip(hint)
            self.rate_group.addButton(rb, i)
            grid.addWidget(rb, 0, col)
            col += 1
            if i == 0:
                rb.setChecked(True)
        grid.addWidget(self.custom_e, 0, col)
        self.rate_group.idToggled.connect(lambda *_: self.refresh())
        vbox.addLayout(grid)

        self.preview = WWLabel('')
        vbox.addWidget(self.preview)
        self.warning = WWLabel('')
        self.warning.setStyleSheet('color: #b06000')
        vbox.addWidget(self.warning)

        self.prepare_btn = QPushButton(_('Prepare now'))
        self.prepare_btn.clicked.connect(self.prepare_now)
        self.queue_btn = QPushButton(_('Queue for low tide'))
        self.queue_btn.clicked.connect(self.queue)
        vbox.addLayout(Buttons(CancelButton(self), self.queue_btn, self.prepare_btn))
        self.plans = []
        self.refresh()

    # --- state --------------------------------------------------------

    def selected_groups(self) -> List[C.Group]:
        keys = set()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.CheckState.Checked:
                keys.add(it.data(0, Qt.ItemDataRole.UserRole))
        return [g for g in self.groups if g.key in keys]

    def rate(self) -> Optional[float]:
        i = self.rate_group.checkedId()
        if i < 0:
            return None
        name, rate, _hint = self.rate_options[i]
        if rate is None:
            try:
                rate = float(self.custom_e.text().replace(',', '.'))
            except ValueError:
                return None
        return max(H.FLOOR_RATE, H.quantize(rate))

    def refresh(self):
        groups = self.selected_groups()
        rate = self.rate()
        self.plans = C.make_plan(groups, rate, merge=self.merge_cb.isChecked(), output_type=self.output_type) if rate else []
        st = self.plugin.state
        lines = []
        for p in self.plans:
            names = ', '.join(f"'{g.title}'" for g in p.groups)
            e = st.eta(p.rate)
            eta = self.plugin.fmt_eta(e, short=True) if e else ''
            lines.append(f"<b>{names}</b>: {len(p.coins)} {_('coins')} → 1 {_('for')} <b>{fmt_sats_fiat(self.window, p.fee)}</b> · {eta}")
            if p.uneconomical:
                lines.append(f"&nbsp;&nbsp;{len(p.uneconomical)} {_('of these coins cost more to move than they hold even at this rate; they are included because moving them now is cheapest.')}")
        if self.plans:
            spike = max((st.spikes or {2024: 1190.0}).values())
            total_vb = sum(p.vsize for p in self.plans)
            lines.append('<br>' + _('Contrast') + ': ' + C.contrast(total_vb, rate, spike))
        elif groups:
            lines.append(_('Nothing to consolidate: select groups with at least two coins, or merge groups.'))
        else:
            lines.append(_('Select at least one group.'))
        self.preview.setText('<br>'.join(lines))
        warn = C.linking_warning(groups) if (self.merge_cb.isChecked() or len(groups) == 1) else ''
        if self.migration and self.dest_e is not None:
            addr = self.dest_e.text().strip()
            if not addr:
                warn = _('Enter the destination address of your new native segwit wallet.')
            elif not is_address(addr):
                warn = _('That is not a valid address for this network.')
            elif self.wallet.is_mine(addr):
                warn = _('That address belongs to this wallet; migration needs the new wallet.')
            elif not addr.lower().startswith(('bc1', 'tb1')):
                warn = _('That is not a native segwit address.')
        self.warning.setText(warn)
        ok = bool(self.plans) and not (self.migration and (not self.dest_e.text().strip() or not is_address(self.dest_e.text().strip())))
        self.prepare_btn.setEnabled(ok)
        self.queue_btn.setEnabled(ok)

    # --- actions ------------------------------------------------------

    def _destination(self) -> str:
        if self.migration:
            return self.dest_e.text().strip()
        return self.wallet.get_unused_address() or self.wallet.get_receiving_address()

    def prepare_now(self):
        plans = list(self.plans)
        self.accept()
        for p in plans:
            try:
                tx = self.build_tx(p)
            except Exception as e:
                self.window.show_error(_('Could not build the transaction') + f': {e}')
                return
            self.window.show_transaction(tx)

    def build_tx(self, p: C.Plan):
        inputs = [self.by_outpoint[c.outpoint] for c in p.coins]
        addr = self._destination()
        outputs = [PartialTxOutput.from_address_and_value(addr, '!')]
        fee_policy = FeePolicy(f'feerate:{int(round(p.rate * 1000))}')
        tx = self.wallet.make_unsigned_transaction(coins=inputs, outputs=outputs, fee_policy=fee_policy, rbf=True)
        return tx

    def queue(self):
        plans = list(self.plans)
        storage = self.plugin.get_storage(self.wallet)
        queue = storage.setdefault('queue', [])
        for p in plans:
            queue.append({
                'id': uuid.uuid4().hex[:12],
                'kind': 'migration' if self.migration else 'consolidation',
                'label': ' + '.join(g.title for g in p.groups),
                'outpoints': [c.outpoint for c in p.coins],
                'address': self._destination() if self.migration else None,
                'amount_sat': None,
                'deadline': None,
                'max_feerate': None,
                'created': int(time.time()),
                'status': 'queued',
            })
        self.wallet.save_db()
        self.accept()
        self.plugin.on_queue_changed(self.window)
        self.window.show_message(_('Queued {} consolidation(s). LowTide will prompt you to sign when the next low tide starts.').format(len(plans)), title='LowTide')
