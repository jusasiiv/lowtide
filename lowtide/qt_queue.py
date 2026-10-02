"""Send-later queue dialog: queued payments and consolidations, their planned windows, and the demo switches."""
import time
import uuid
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import Qt, QDateTime
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QPushButton, QHeaderView,
                             QAbstractItemView, QLineEdit, QDateTimeEdit, QCheckBox, QGridLayout, QGroupBox)

from electrum.i18n import _
from electrum.bitcoin import is_address
from electrum.bip21 import parse_bip21_URI
from electrum.gui.qt.util import WindowModalDialog, Buttons, CloseButton, WWLabel
from electrum.gui.qt.amountedit import BTCAmountEdit

from .core import planner as P
from .fmt import fmt_rate, fmt_time, fmt_duration

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow


def parse_payto(text: str):
    """(address, amount_sat or None, label or '') from an address or a BIP21 URI."""
    text = (text or '').strip()
    if not text:
        return None, None, ''
    if text.lower().startswith('bitcoin:'):
        try:
            d = parse_bip21_URI(text)
        except Exception:
            return None, None, ''
        return d.get('address'), d.get('amount'), d.get('message') or d.get('label') or ''
    if is_address(text):
        return text, None, ''
    return None, None, ''


class QueueDialog(WindowModalDialog):

    def __init__(self, plugin, window: 'ElectrumWindow', *, prefill: Optional[dict] = None):
        WindowModalDialog.__init__(self, window, 'LowTide — ' + _('Send later'))
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.setMinimumWidth(860)
        v = QVBoxLayout(self)
        v.addWidget(WWLabel(_('Queued payments are sent in one batch when the forecast says it is cheapest before their deadline. '
                              'LowTide prompts you to sign; it never signs or broadcasts on its own.')))

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([_('Label'), _('To'), _('Amount'), _('Confirmed by'), _('Planned'), _('Rate'), _('Status')])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(160)
        v.addWidget(self.table)

        row = QHBoxLayout()
        self.remove_btn = QPushButton(_('Remove selected'))
        self.remove_btn.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        self.send_btn = QPushButton(_('Send due items now'))
        self.send_btn.clicked.connect(lambda: self.plugin.send_due(self.window, force=False, parent=self))
        row.addWidget(self.send_btn)
        self.send_all_btn = QPushButton(_('Send everything now'))
        self.send_all_btn.setToolTip(_('Ignore the plan and build the batch now at a rate that meets every deadline.'))
        self.send_all_btn.clicked.connect(lambda: self.plugin.send_due(self.window, force=True, parent=self))
        row.addWidget(self.send_all_btn)
        v.addLayout(row)

        box = QGroupBox(_('Queue a payment'))
        g = QGridLayout(box)
        g.addWidget(QLabel(_('Pay to')), 0, 0)
        self.payto_e = QLineEdit()
        self.payto_e.setPlaceholderText(_('address or bitcoin: URI'))
        g.addWidget(self.payto_e, 0, 1, 1, 3)
        g.addWidget(QLabel(_('Amount')), 1, 0)
        self.amount_e = BTCAmountEdit(window.get_decimal_point)
        g.addWidget(self.amount_e, 1, 1)
        g.addWidget(QLabel(_('Label')), 1, 2)
        self.label_e = QLineEdit()
        g.addWidget(self.label_e, 1, 3)
        g.addWidget(QLabel(_('Confirmed by')), 2, 0)
        self.deadline_e = QDateTimeEdit(QDateTime.currentDateTime().addDays(3))
        self.deadline_e.setCalendarPopup(True)
        self.deadline_e.setDisplayFormat('ddd dd MMM HH:mm')
        g.addWidget(self.deadline_e, 2, 1)
        self.no_deadline_cb = QCheckBox(_('no deadline (cheapest window in the next 7 days)'))
        self.no_deadline_cb.toggled.connect(lambda on: self.deadline_e.setEnabled(not on))
        g.addWidget(self.no_deadline_cb, 2, 2, 1, 2)
        g.addWidget(QLabel(_('Max fee rate (optional)')), 3, 0)
        self.max_e = QLineEdit()
        self.max_e.setPlaceholderText('sat/vB')
        self.max_e.setFixedWidth(90)
        g.addWidget(self.max_e, 3, 1)
        self.add_btn = QPushButton(_('Add to queue'))
        self.add_btn.clicked.connect(self.add_item)
        g.addWidget(self.add_btn, 3, 3, alignment=Qt.AlignmentFlag.AlignRight)
        v.addWidget(box)
        if prefill:
            self.payto_e.setText(prefill.get('payto') or '')
            if prefill.get('amount_sat') not in (None, '!'):
                self.amount_e.setAmount(prefill['amount_sat'])
            self.label_e.setText(prefill.get('label') or '')

        if plugin.demo_mode():
            demo = QGroupBox(_('Demo switches'))
            h = QHBoxLayout(demo)
            self.lt_cb = QCheckBox(_('Low tide now'))
            self.lt_cb.setChecked(bool(plugin.state.demo.get('low_tide_now')))
            self.lt_cb.toggled.connect(lambda on: plugin.set_demo('low_tide_now', on))
            h.addWidget(self.lt_cb)
            self.dl_cb = QCheckBox(_('Deadline approaching'))
            self.dl_cb.setChecked(bool(plugin.state.demo.get('deadline_soon')))
            self.dl_cb.toggled.connect(lambda on: plugin.set_demo('deadline_soon', on))
            h.addWidget(self.dl_cb)
            self.st_cb = QCheckBox(_('Stranded transaction'))
            self.st_cb.setChecked(bool(plugin.state.demo.get('stranded_now')))
            self.st_cb.toggled.connect(lambda on: plugin.set_demo('stranded_now', on))
            h.addWidget(self.st_cb)
            h.addStretch(1)
            reset = QPushButton(_('Demo reset'))
            reset.clicked.connect(lambda: (plugin.demo_reset(), self.refresh()))
            h.addWidget(reset)
            v.addWidget(demo)

        self.note = WWLabel('')
        self.note.setStyleSheet('color: gray')
        v.addWidget(self.note)
        v.addLayout(Buttons(CloseButton(self)))
        self.refresh()

    # --- data ---------------------------------------------------------

    def items(self) -> List[dict]:
        return self.plugin.planned_queue(self.wallet)

    def refresh(self):
        items = self.items()
        self.table.setRowCount(len(items))
        now = time.time()
        for i, it in enumerate(items):
            to = it.get('address') or (_('this wallet') if it.get('kind') == 'consolidation' else '')
            if to and len(to) > 16:
                to = to[:10] + '…' + to[-4:]
            amt = f"{it['amount_sat']:,} sats" if isinstance(it.get('amount_sat'), int) else (
                _('all of') + f" {len(it.get('outpoints') or [])} " + _('coins') if it.get('outpoints') else _('max'))
            dl = fmt_time(it['deadline']) if it.get('deadline') else _('none')
            planned = ''
            if it.get('status') == 'queued' and it.get('planned_start'):
                planned = fmt_time(it['planned_start']) + (f" ({_('in')} {fmt_duration(it['planned_start'] - now)})" if it['planned_start'] > now + 60 else f" ({_('due')})")
            rate = fmt_rate(it['planned_rate']) + ' sat/vB' if it.get('planned_rate') else ''
            status = it.get('status', '')
            if status == 'sent' and it.get('txid'):
                status = _('sent') + ' ' + it['txid'][:8] + '…'
            cells = [it.get('label') or it.get('kind', ''), to, amt, dl, planned, rate, status]
            for j, c in enumerate(cells):
                cell = QTableWidgetItem(c)
                cell.setToolTip(it.get('plan_note') or '')
                cell.setData(Qt.ItemDataRole.UserRole, it['id'])
                self.table.setItem(i, j, cell)
        due = P.due_items(items, now, demo=self.plugin.state.demo)
        self.send_btn.setEnabled(bool(due))
        self.send_all_btn.setEnabled(any(it.get('status') == 'queued' for it in items))
        if due:
            self.note.setText(_('{} item(s) are due: the batch is ready to prepare.').format(len(due)))
        elif items:
            nxt = min((it['planned_start'] for it in items if it.get('status') == 'queued' and it.get('planned_start')), default=None)
            self.note.setText(_('Next batch planned for {}.').format(fmt_time(nxt)) if nxt else '')
        else:
            self.note.setText(_('Nothing queued yet. Fill in a payment below, or use "Later…" in the Send tab.'))

    def selected_ids(self) -> List[str]:
        ids = set()
        for it in self.table.selectedItems():
            ids.add(it.data(Qt.ItemDataRole.UserRole))
        return list(ids)

    def remove_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        self.plugin.remove_queue_items(self.wallet, ids)
        self.refresh()

    def add_item(self):
        addr, uri_amount, uri_label = parse_payto(self.payto_e.text())
        if not addr:
            self.show_error(_('Enter a valid address or bitcoin: URI.'))
            return
        amount = self.amount_e.get_amount() or uri_amount
        if not amount:
            self.show_error(_('Enter an amount.'))
            return
        deadline = None if self.no_deadline_cb.isChecked() else self.deadline_e.dateTime().toSecsSinceEpoch()
        if deadline is not None and deadline < time.time() + 1800:
            self.show_error(_('The deadline must be at least 30 minutes away.'))
            return
        cap = None
        if self.max_e.text().strip():
            try:
                cap = float(self.max_e.text().replace(',', '.'))
            except ValueError:
                self.show_error(_('Max fee rate must be a number.'))
                return
        item = {
            'id': uuid.uuid4().hex[:12], 'kind': 'payment', 'address': addr, 'amount_sat': int(amount),
            'label': self.label_e.text().strip() or uri_label, 'deadline': int(deadline) if deadline else None,
            'max_feerate': cap, 'created': int(time.time()), 'status': 'queued',
        }
        self.plugin.add_queue_item(self.wallet, item)
        self.payto_e.clear()
        self.amount_e.setAmount(None)
        self.label_e.clear()
        self.refresh()
