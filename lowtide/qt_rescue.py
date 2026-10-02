"""Stuck transaction rescue dialog and the accelerator flow."""
import time
from functools import partial
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import Qt, QDateTime, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QPushButton,
                             QHeaderView, QAbstractItemView, QDateTimeEdit, QCheckBox, QRadioButton, QButtonGroup,
                             QWidget, QGridLayout)

from electrum.i18n import _
from electrum.transaction import Transaction, PartialTransaction
from electrum.wallet import CannotBumpFee, CannotCPFP
from electrum.gui.qt.util import WindowModalDialog, Buttons, CloseButton, CancelButton, OkButton, WWLabel
from electrum.gui.qt.qrcodewidget import QRCodeWidget
from electrum.gui.common_qt.util import TaskThread

from .core import rescue as R
from .core import accelerator as A
from .fmt import fmt_rate, fmt_sats_fiat, fmt_duration, fmt_time
from .qt_wallet import wallet_script_info

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow

WHEN_TO_ACCELERATE = [
    (_("You can't sign"), _("a watch-only merchant wallet, a multisig cosigner who is away, a hardware wallet that isn't at hand, "
                             "or pre-signed transactions such as Timelock Recovery plans or Lightning force-closes.")),
    (_("The txid must not change"), _("invoices, exchange deposits and payment proofs track the txid, and RBF changes it.")),
    (_("It's an incoming payment"), _("someone else underpaid; you cannot replace their transaction.")),
    (_("Your coins are tied up, but you have Lightning balance"), _("CPFP needs spare on-chain coins; the accelerator takes a Lightning payment.")),
]


def tx_facts(plugin, window, tx: Transaction) -> R.TxFacts:
    wallet = window.wallet
    info = wallet.get_tx_info(tx)
    fee = info.fee
    if fee is None:
        fee = wallet.adb.get_tx_fee(tx.txid())
    vsize = tx.estimated_size()
    ts = info.tx_mined_status.timestamp
    first_seen = plugin.first_seen(wallet, tx.txid())
    age = time.time() - (first_seen or ts or time.time())
    script_type, _m, _n = wallet_script_info(wallet)
    ln = 0
    try:
        if wallet.lnworker:
            ln = int(wallet.lnworker.num_sats_can_send())
    except Exception:
        ln = 0
    delta = wallet.get_wallet_delta(tx)
    relay = plugin.relay_rate()
    return R.TxFacts(txid=tx.txid(), vsize=vsize, fee=int(fee) if fee is not None else -1, age_s=age, can_bump=bool(info.can_bump),
                     can_cpfp=bool(info.can_cpfp), script_type=script_type, has_lightning_sats=ln,
                     incoming=not delta.is_any_input_ismine, relay_rate=relay)


class RescueDialog(WindowModalDialog):

    def __init__(self, plugin, window: 'ElectrumWindow', tx: Transaction):
        WindowModalDialog.__init__(self, window, 'LowTide — ' + _('Rescue stuck transaction'))
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.tx = tx
        self.setMinimumWidth(820)
        self.facts = tx_facts(plugin, window, tx)
        self.estimate = None  # type: Optional[A.Estimate]
        self.estimate_error = ''
        self.options = []  # type: List[R.Option]
        st = plugin.state

        vbox = QVBoxLayout(self)
        f = self.facts
        kind = _('incoming payment') if f.incoming else _('your payment')
        self.head = QLabel('')
        vbox.addWidget(self.head)
        self._update_head()
        self.safety = WWLabel('')
        self.safety.setStyleSheet('color: #b06000')
        vbox.addWidget(self.safety)

        hb = QHBoxLayout()
        self.deadline_cb = QCheckBox(_('Must confirm by'))
        self.deadline_cb.setChecked(True)
        self.deadline_cb.toggled.connect(self.refresh)
        hb.addWidget(self.deadline_cb)
        self.deadline_e = QDateTimeEdit(QDateTime.currentDateTime().addSecs(6 * 3600))
        self.deadline_e.setCalendarPopup(True)
        self.deadline_e.setDisplayFormat('ddd dd MMM HH:mm')
        self.deadline_e.dateTimeChanged.connect(self.refresh)
        hb.addWidget(self.deadline_e)
        hb.addStretch(1)
        vbox.addLayout(hb)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([_('Option'), _('Cost'), _('Confirms'), _('Needs'), ''])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setMinimumHeight(200)
        vbox.addWidget(self.table)

        self.reco = WWLabel('')
        vbox.addWidget(self.reco)

        when = '<b>' + _('When is acceleration the right call?') + '</b><ul>' + ''.join(
            f'<li><b>{t}</b>: {d}</li>' for t, d in WHEN_TO_ACCELERATE) + '</ul>'
        when_label = WWLabel(when)
        when_label.setStyleSheet('color: gray')
        vbox.addWidget(when_label)
        vbox.addLayout(Buttons(CloseButton(self)))

        self.estimating = False
        if not plugin.accelerator_available():
            self.estimate_error = _('mainnet only')
        self.refresh()

    def _update_head(self):
        f = self.facts
        kind = _('incoming payment') if f.incoming else _('your payment')
        rate = f"<b>{fmt_rate(f.rate)} sat/vB</b> ({f.fee:,} sats, {f.vsize} vB)" if f.fee >= 0 else _('fee unknown until the estimate is fetched')
        self.head.setText(f"<b>{self.tx.txid()[:16]}…</b> — {kind}, {rate}, {_('unconfirmed for')} {fmt_duration(f.age_s)}")

    # --- data ---------------------------------------------------------

    def _fetch_estimate(self):
        if not self.plugin.network_available():
            self.estimate_error = _('offline')
            self.refresh()
            return
        client = self.plugin.accelerator_client()
        txid = self.tx.txid()
        self.estimate_error = ''
        self.estimating = True
        self.refresh()
        def task():
            return client.estimate(txid)
        def on_success(est):
            self.estimating = False
            self.estimate = est
            if est.unavailable:
                self.estimate_error = _('not eligible for acceleration')
            if self.facts.fee < 0 and est.effective_fee:
                self.facts = self.facts._replace(fee=est.effective_fee, vsize=est.effective_vsize or self.facts.vsize)
                self._update_head()
            self.refresh()
        def on_error(exc_info):
            self.estimating = False
            self.estimate_error = _('estimate unavailable') + f' ({exc_info[1]})'
            self.refresh()
        self._thread = TaskThread(self)
        self._thread.add(task, on_success, self._thread.stop, on_error)

    def deadline_s(self) -> Optional[float]:
        if not self.deadline_cb.isChecked():
            return None
        return max(600.0, self.deadline_e.dateTime().toSecsSinceEpoch() - time.time())

    def refresh(self):
        st = self.plugin.state
        f = self.facts
        dl = self.deadline_s()
        deadline_rate = None
        if dl is not None and st.hist:
            e = st.rate_for_deadline(dl)
            deadline_rate = e.rate if e else None
        accel_total, accel_reason, accel_eta = None, self.estimate_error, None
        if self.estimate and not self.estimate.unavailable:
            accel_total = self.estimate.total(self.estimate.options[0])
            if st.next_block_rate is not None:
                accel_eta = st.eta(max(st.next_block_rate, self.estimate.target_rate))
        elif self.estimating:
            accel_reason = _('estimating…')
        elif not self.estimate_error:
            accel_reason = _('cost not fetched yet')
        self.options = R.compare(f, eta_fn=lambda r: st.eta(r), next_block_rate=st.next_block_rate, pile_rate=st.pile.rate if st.pile else None,
                                 deadline_rate=deadline_rate, mempool_min_fee=st.mempool_min_fee, accel_total=accel_total,
                                 accel_reason=accel_reason, accel_eta=accel_eta)
        rec = R.recommend(self.options, dl)
        wait = self.options[0]
        self.safety.setText(('⚠ ' + wait.reason) if wait.reason else '')
        self.safety.setVisible(bool(wait.reason))

        self.table.setRowCount(len(self.options))
        for i, o in enumerate(self.options):
            cost = fmt_sats_fiat(self.window, o.cost_sats) if o.cost_sats is not None else '—'
            if o.key == 'wait':
                cost = _('free')
            conf = self.plugin.fmt_eta(o.eta, short=True) if o.eta else '—'
            needs = []
            if o.needs_signing:
                needs.append(_('signing'))
            if o.changes_txid:
                needs.append(_('new txid'))
            if o.key == 'accelerate':
                needs.append(_('Lightning or any wallet'))
            if o.key == 'cpfp':
                needs.append(_('spare output'))
            title = o.title + (f" → {fmt_rate(o.new_rate)} sat/vB" if o.new_rate and o.key != 'wait' else '')
            cells = [title, cost, conf, ', '.join(needs) or '—']
            for j, v in enumerate(cells):
                it = QTableWidgetItem(v)
                it.setToolTip(o.detail or o.reason)
                if not o.available:
                    it.setForeground(Qt.GlobalColor.gray)
                    if j == 0:
                        it.setText(f'{o.title} ({o.reason})')
                elif rec.option is o:
                    font = it.font(); font.setBold(True); it.setFont(font)
                self.table.setItem(i, j, it)
            if o.key == 'accelerate' and self.estimate is None and not self.estimate_error:
                btn = QPushButton(_('Get estimate'))
                btn.setToolTip(_('Sends this txid to mempool.space to price an acceleration.'))
                btn.setEnabled(not self.estimating)
                btn.clicked.connect(self._fetch_estimate)
            else:
                btn = QPushButton(_('Choose'))
                btn.setEnabled(o.available and o.key != 'wait')
                btn.clicked.connect(partial(self._choose, o))
            self.table.setCellWidget(i, 4, btn)
        if rec.option:
            self.reco.setText(f"<b>{_('Recommended')}: {rec.option.title}</b> — {rec.why}")
        else:
            self.reco.setText(rec.why)

    # --- actions ------------------------------------------------------

    def _choose(self, o: R.Option):
        if o.key in ('small_rbf', 'rbf'):
            self._bump(o.new_rate)
        elif o.key == 'cpfp':
            self._cpfp(o.cost_sats)
        elif o.key == 'accelerate':
            self._accelerate()

    def _bump(self, rate: float):
        tx = self.tx
        if not isinstance(tx, PartialTransaction):
            tx = PartialTransaction.from_tx(tx)
        if not tx.add_info_from_wallet_and_network(wallet=self.wallet, show_error=self.show_error):
            return
        try:
            new_tx = self.wallet.bump_fee(tx=tx, new_fee_rate=rate, coins=self.window.get_coins(nonlocal_only=True))
        except CannotBumpFee as e:
            self.show_error(_('Cannot bump fee') + ': ' + str(e))
            return
        self.accept()
        self.window.show_transaction(new_tx)

    def _cpfp(self, child_fee: int):
        try:
            new_tx = self.wallet.cpfp(self.tx, child_fee)
        except CannotCPFP as e:
            self.show_error(_('Cannot CPFP') + ': ' + str(e))
            return
        self.accept()
        self.window.show_transaction(new_tx)

    def _accelerate(self):
        if self.estimate is None:
            return
        self.accept()
        d = AccelerateDialog(self.plugin, self.window, self.tx, self.estimate)
        d.exec()


class AccelerateDialog(WindowModalDialog):
    """Estimate → bid → explicit confirm → invoice → pay (Lightning here, or QR for any wallet) → track."""

    ln_result = pyqtSignal(object)

    def __init__(self, plugin, window: 'ElectrumWindow', tx: Transaction, estimate: A.Estimate):
        WindowModalDialog.__init__(self, window, 'LowTide — ' + _('mempool Accelerator'))
        self.ln_result.connect(self._on_ln_result, Qt.ConnectionType.QueuedConnection)
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.tx = tx
        self.txid = tx.txid()
        self.est = estimate
        self.client = plugin.accelerator_client()
        self.invoice = None  # type: Optional[A.Invoice]
        self.invoice_id = None
        self.setMinimumWidth(640)
        v = QVBoxLayout(self)
        v.addWidget(WWLabel(_('mempool.space pays miners out-of-band to include your transaction in the next blocks. '
                              'No signing, the txid does not change, and it works for incoming payments. '
                              'The txid is sent to mempool.space when you continue.')))
        grid = QGridLayout()
        grid.addWidget(QLabel(_('Transaction')), 0, 0)
        grid.addWidget(QLabel(f'{self.txid[:16]}… ({fmt_rate(estimate.current_rate)} sat/vB, {estimate.effective_vsize} vB)'), 0, 1)
        grid.addWidget(QLabel(_('Bid')), 1, 0)
        self.bid_group = QButtonGroup(self)
        hb = QHBoxLayout()
        for i, fee in enumerate(estimate.options):
            rb = QRadioButton(f'{fee:,} sats')
            self.bid_group.addButton(rb, i)
            hb.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        hb.addStretch(1)
        grid.addLayout(hb, 1, 1)
        grid.addWidget(QLabel(_('Base fee')), 2, 0)
        grid.addWidget(QLabel(f'{estimate.base_fee:,} sats' + (f' + {estimate.vsize_fee:,} sats {_("size fee")}' if estimate.vsize_fee else '')), 2, 1)
        grid.addWidget(QLabel(_('Total')), 3, 0)
        self.total_label = QLabel('')
        f = self.total_label.font(); f.setBold(True); self.total_label.setFont(f)
        grid.addWidget(self.total_label, 3, 1)
        v.addLayout(grid)
        self.bid_group.idToggled.connect(lambda *_: self._update_total())
        self._update_total()

        self.status_label = WWLabel('')
        v.addWidget(self.status_label)
        self.qr = QRCodeWidget('', manual_size=True)
        self.qr.setFixedSize(260, 260)
        self.qr.hide()
        v.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignCenter)
        self.bolt11_label = WWLabel('')
        self.bolt11_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.bolt11_label.hide()
        v.addWidget(self.bolt11_label)

        self.continue_btn = QPushButton(_('Continue: create invoice'))
        self.continue_btn.clicked.connect(self._create_invoice)
        self.pay_ln_btn = QPushButton(_('Pay with this wallet\'s Lightning'))
        self.pay_ln_btn.clicked.connect(self._pay_lightning)
        self.pay_ln_btn.hide()
        self.copy_btn = QPushButton(_('Copy invoice'))
        self.copy_btn.clicked.connect(lambda: self.window.do_copy(self.invoice.bolt11 if self.invoice else '', title=_('Lightning invoice')))
        self.copy_btn.hide()
        v.addLayout(Buttons(CancelButton(self), self.copy_btn, self.pay_ln_btn, self.continue_btn))
        self._poll = QTimer(self)
        self._poll.setInterval(15_000)
        self._poll.timeout.connect(self._check_status)

    def bid(self) -> int:
        return self.est.options[max(0, self.bid_group.checkedId())]

    def total(self) -> int:
        return self.est.total(self.bid())

    def _update_total(self):
        self.total_label.setText(fmt_sats_fiat(self.window, self.total()))

    def _create_invoice(self):
        total = self.total()
        if not self.question(_('Create an acceleration invoice for {}?\n\nNothing is paid yet. You pay in the next step, and only after confirming again.').format(
                fmt_sats_fiat(self.window, total)), title=_('Confirm cost')):
            return
        self.continue_btn.setEnabled(False)
        self.status_label.setText(_('Creating invoice…'))
        bid = self.bid()
        def task():
            iid = self.client.create_invoice(self.txid, bid)
            return iid, self.client.invoice(iid)
        def on_success(res):
            iid, inv = res
            self.invoice_id, self.invoice = iid, inv
            self._show_invoice()
        def on_error(exc_info):
            self.status_label.setText(_('Could not create the invoice') + f': {exc_info[1]}')
            self.continue_btn.setEnabled(True)
        self._thread = TaskThread(self)
        self._thread.add(task, on_success, self._thread.stop, on_error)

    def _show_invoice(self):
        inv = self.invoice
        total = self.total()
        quoted = A.bolt11_amount_sat(inv.bolt11) if inv.bolt11 else inv.amount_sat
        mismatch = quoted is not None and quoted != total
        txt = f"{_('Invoice')} {inv.invoice_id}: <b>{quoted:,} sats</b>" if quoted is not None else _('Invoice created')
        if mismatch:
            txt += ' ⚠ ' + _('does not match the quoted total of {} sats; do not pay').format(f'{total:,}')
        if inv.expires_at:
            txt += f" · {_('expires')} {fmt_time(inv.expires_at)}"
        self.status_label.setText(txt)
        self.plugin.record_acceleration(self.wallet, self.txid, {
            'invoice_id': inv.invoice_id, 'bolt11': inv.bolt11, 'bid': self.bid(), 'total': total, 'quoted': quoted,
            'created': int(time.time()), 'paid': False, 'status': 'invoice', 'estimate': self.est.raw,
        })
        if inv.bolt11:
            self.qr.setData(inv.bolt11.upper())
            self.qr.show()
            self.bolt11_label.setText(inv.bolt11[:60] + '…')
            self.bolt11_label.show()
            self.copy_btn.show()
            if not mismatch and self._can_pay_lightning(quoted or total):
                self.pay_ln_btn.show()
            elif not mismatch:
                self.status_label.setText(self.status_label.text() + '<br>' + _('Pay this invoice with any Lightning wallet; LowTide tracks the acceleration.'))
        else:
            self.status_label.setText(self.status_label.text() + '<br>' + _('No Lightning invoice in the response; see the raw invoice in the log.'))
            self.plugin.logger.info(f'accelerator invoice raw: {inv.raw}')
        self.continue_btn.hide()
        self._poll.start()

    def _can_pay_lightning(self, amount_sat: int) -> bool:
        ln = self.wallet.lnworker
        if ln is None:
            return False
        try:
            return int(ln.num_sats_can_send()) >= amount_sat
        except Exception:
            return False

    def _pay_lightning(self):
        inv = self.invoice
        total = A.bolt11_amount_sat(inv.bolt11) or self.total()
        if not self.question(_('Pay {} from this wallet\'s Lightning balance to accelerate {}?\n\nThis is the final confirmation.').format(
                fmt_sats_fiat(self.window, total), self.txid[:16] + '…'), title=_('Confirm payment')):
            return
        from electrum.invoices import Invoice
        try:
            electrum_invoice = Invoice.from_bech32(inv.bolt11)
        except Exception as e:
            self.show_error(_('Cannot parse the invoice') + f': {e}')
            return
        self.pay_ln_btn.setEnabled(False)
        self.status_label.setText(_('Paying over Lightning…'))
        coro = self.wallet.lnworker.pay_invoice(electrum_invoice)
        self.window.run_coroutine_from_thread(coro, _('LowTide: paying acceleration'), on_result=self.ln_result.emit)

    def _on_ln_result(self, res):
        try:
            ok = bool(res[0]) if isinstance(res, tuple) else bool(res)
        except Exception:
            ok = False
        if ok:
            self.status_label.setText(_('Paid. Waiting for the acceleration to be confirmed…'))
            self.plugin.record_acceleration(self.wallet, self.txid, {'paid': True, 'status': 'paid', 'paid_at': int(time.time())})
        else:
            self.status_label.setText(_('Lightning payment failed; you can still pay the invoice with another wallet.'))
            self.pay_ln_btn.setEnabled(True)

    def _check_status(self):
        txid, iid = self.txid, self.invoice_id
        client = self.client
        def task():
            paid = client.is_paid(iid) if iid else False
            return paid, client.status(txid)
        def on_success(res):
            paid, st = res
            rec = {'paid': paid or None}
            if st:
                rec['status'] = st.status
                if st.block_height:
                    rec['block_height'] = st.block_height
            self.plugin.record_acceleration(self.wallet, txid, {k: v for k, v in rec.items() if v is not None})
            if st and st.done:
                self.status_label.setText(_('Accelerated and mined in block {}.').format(st.block_height or '?'))
                self._poll.stop()
            elif st:
                self.status_label.setText(_('Acceleration status') + f': {st.status}')
            elif paid:
                self.status_label.setText(_('Payment received; acceleration pending.'))
        self._thread2 = TaskThread(self)
        self._thread2.add(task, on_success, self._thread2.stop, lambda e: None)

    def closeEvent(self, ev):
        self._poll.stop()
        WindowModalDialog.closeEvent(self, ev)
