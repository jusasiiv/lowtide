"""mempool.space transaction accelerator client (mainnet only). Transport injected; never pays by itself."""
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional

HttpGet = Callable[[str], Any]
HttpPost = Callable[[str, dict], Any]

ACTIVE_STATUSES = {'requested', 'accelerating', 'pending', 'mined', 'completed_provisional'}
DONE_STATUSES = {'completed', 'completed_provisional', 'mined', 'mined_provisional'}
FAILED_STATUSES = {'failed', 'failed_provisional', 'canceled', 'cancelled', 'expired'}


class Estimate(NamedTuple):
    txid: str
    cost: int                 # mempool's suggested bid
    options: List[int]        # selectable bids (sats)
    base_fee: int             # mempoolBaseFee
    vsize_fee: int
    target_rate: float
    next_block_fee: int
    effective_vsize: int
    effective_fee: int
    unavailable: bool
    bitcoin_min: int
    bitcoin_max: int
    raw: dict

    def total(self, bid: int) -> int:
        """What the user pays: bid + base fee + vsize fee (mempool's frontend formula)."""
        return int(bid) + self.base_fee + self.vsize_fee

    @property
    def current_rate(self) -> float:
        return self.effective_fee / self.effective_vsize if self.effective_vsize else 0.0


def parse_estimate(txid: str, d: dict) -> Estimate:
    if not isinstance(d, dict) or 'options' not in d and 'cost' not in d:
        raise ValueError(f'unexpected estimate response: {str(d)[:200]}')
    summ = d.get('txSummary') or {}
    opts = [int(o.get('fee', 0)) for o in (d.get('options') or []) if isinstance(o, dict)]
    pay = ((d.get('availablePaymentMethods') or {}).get('bitcoin') or {})
    return Estimate(
        txid=txid, cost=int(d.get('cost') or (opts[0] if opts else 0)), options=opts or [int(d.get('cost') or 0)],
        base_fee=int(d.get('mempoolBaseFee') or 0), vsize_fee=int(d.get('vsizeFee') or 0),
        target_rate=float(d.get('targetFeeRate') or 0), next_block_fee=int(d.get('nextBlockFee') or 0),
        effective_vsize=int(summ.get('effectiveVsize') or 0), effective_fee=int(summ.get('effectiveFee') or 0),
        unavailable=bool(d.get('unavailable')), bitcoin_min=int(pay.get('min') or 0), bitcoin_max=int(pay.get('max') or 0), raw=d,
    )


class Invoice(NamedTuple):
    invoice_id: str
    bolt11: Optional[str]
    amount_sat: Optional[int]
    onchain_address: Optional[str]
    expires_at: Optional[int]
    raw: Any


_BOLT11_RE = re.compile(r'\b(ln(?:bc|tb|bcrt)[0-9a-z]{50,})\b', re.I)


def _find_bolt11(obj: Any) -> Optional[str]:
    """Find a BOLT11 string anywhere in a nested response."""
    if isinstance(obj, str):
        m = _BOLT11_RE.search(obj)
        return m.group(1) if m else None
    if isinstance(obj, dict):
        for k in ('BOLT11', 'bolt11', 'lightning', 'paymentRequest', 'payment_request', 'destination'):
            v = obj.get(k)
            if isinstance(v, str) and _BOLT11_RE.search(v):
                return _BOLT11_RE.search(v).group(1)
        for v in obj.values():
            r = _find_bolt11(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_bolt11(v)
            if r:
                return r
    return None


def _find_key(obj: Any, keys) -> Optional[Any]:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ''):
                return obj[k]
        for v in obj.values():
            r = _find_key(v, keys)
            if r is not None:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_key(v, keys)
            if r is not None:
                return r
    return None


def parse_invoice(invoice_id: str, d: Any) -> Invoice:
    bolt11 = _find_bolt11(d)
    amount = None
    btc_due = _find_key(d, ('btcDue',))
    if btc_due is not None:
        try:
            amount = int(round(float(btc_due) * 1e8))
        except (TypeError, ValueError):
            amount = None
    if amount is None:
        raw_amt = _find_key(d, ('amount', 'amountSat', 'amount_sat', 'satoshis', 'totalPaid'))
        try:
            amount = int(float(raw_amt)) if raw_amt is not None else None
        except (TypeError, ValueError):
            amount = None
    addr = _find_key(d, ('address', 'onchainAddress', 'btcAddress'))
    exp = _find_key(d, ('expirationTime', 'expiresAt', 'expiry', 'expires'))
    try:
        exp = int(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp = None
    return Invoice(invoice_id=invoice_id, bolt11=bolt11, amount_sat=amount, onchain_address=addr if isinstance(addr, str) else None,
                   expires_at=exp, raw=d)


def bolt11_amount_sat(bolt11: str) -> Optional[int]:
    """Amount encoded in the BOLT11 human-readable part, in sats (None if absent)."""
    m = re.match(r'^ln(bc|tb|bcrt)(\d+)([munp]?)1', bolt11.lower())
    if not m:
        return None
    n = int(m.group(2))
    mult = {'': 1.0, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12}[m.group(3)]
    return int(round(n * mult * 1e8))


class Status(NamedTuple):
    txid: str
    status: str
    done: bool
    failed: bool
    block_height: Optional[int]
    fee_delta: Optional[int]
    bid_boost: Optional[int]
    pool_id: Optional[int]
    raw: Any


def parse_status(txid: str, d: Any) -> Optional[Status]:
    if isinstance(d, list):
        d = next((x for x in d if isinstance(x, dict) and x.get('txid') == txid), None)
    if not isinstance(d, dict):
        return None
    s = str(d.get('status') or '').lower()
    return Status(txid=txid, status=s, done=s in DONE_STATUSES, failed=s in FAILED_STATUSES or bool(d.get('canceled')),
                  block_height=d.get('blockHeight'), fee_delta=d.get('feeDelta'), bid_boost=d.get('bidBoost'),
                  pool_id=d.get('minedByPoolUniqueId'), raw=d)


class AcceleratorClient:
    """Thin client over the public endpoints. All calls are blocking; run them off the GUI thread."""

    def __init__(self, base_url: str, http_get: HttpGet, http_post: HttpPost):
        self.base = base_url.rstrip('/')
        self.get = http_get
        self.post = http_post

    def estimate(self, txid: str) -> Estimate:
        return parse_estimate(txid, self.post(self.base + '/accelerator/estimate', {'txInput': txid}))

    def create_invoice(self, txid: str, bid: int, partner_code: Optional[str] = None) -> str:
        body = {'txid': txid, 'maxBidBoost': int(bid)}
        if partner_code:
            body['partnerCode'] = partner_code
        d = self.post(self.base + '/accelerator/invoice', body)
        iid = (d or {}).get('btcpayInvoiceId') if isinstance(d, dict) else None
        if not iid:
            raise ValueError(f'no invoice id in response: {str(d)[:200]}')
        return str(iid)

    def invoice(self, invoice_id: str) -> Invoice:
        return parse_invoice(invoice_id, self.get(self.base + f'/payments/bitcoin/invoice?id={invoice_id}'))

    def check_payment(self, invoice_id: str) -> Optional[Any]:
        """None while unpaid (the API answers 204 No Content); a JSON record once paid."""
        return self.get(self.base + f'/payments/bitcoin/check?order_id={invoice_id}')

    def is_paid(self, invoice_id: str) -> bool:
        d = self.check_payment(invoice_id)
        if not d:
            return False
        if isinstance(d, dict):
            st = str(d.get('status') or d.get('paid') or '').lower()
            return st in ('paid', 'settled', 'complete', 'completed', 'true', 'confirmed') or bool(d.get('paid'))
        return True

    def status(self, txid: str) -> Optional[Status]:
        try:
            d = self.get(self.base + f'/accelerator/accelerations/{txid}')
        except Exception:
            d = None
        st = parse_status(txid, d)
        if st is None:
            try:
                st = parse_status(txid, self.get(self.base + '/accelerator/accelerations'))
            except Exception:
                st = None
        if st is None:
            try:
                st = parse_status(txid, self.get(self.base + '/accelerator/accelerations/history'))
            except Exception:
                st = None
        return st
