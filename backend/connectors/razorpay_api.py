"""Razorpay public-API client (test/live mode) for scheduled settlement pulls.

Surface:
  - fetch_settlements()  -> canonical Source B rows (/v1/settlements)
  - fetch_payments()     -> canonical Source A rows (/v1/payments, captured,
                            linked to a settlement)
  - derive_bank_rows()   -> Source C rows keyed by settlement UTR (declared
                            derivation: Razorpay does not expose bank
                            statements; real bank feeds stay a CSV upload)

Amounts from the Razorpay API are already integer paise — mapped verbatim.
The transport is injectable (`transport(method, url) -> (status, dict)`) so
tests and the scheduler run without network access.
"""
import base64
import json
import time
import urllib.error
import urllib.request

API_BASE = "https://api.razorpay.com/v1"
SETTLED = {"processed"}
CAPTURED = {"captured"}
MAX_PAGES = 100            # hard pagination ceiling (10k records) — runaway guard
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRIES = 3
BACKOFF_S = 0.75           # exponential: 0.75s, 1.5s, 3s
# Test/scheduler injection point: set to a callable(method,url)->(status,dict).
_transport_override = None


class RazorpayAPIError(RuntimeError):
    """Upstream Razorpay API failure after retries."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def validate_keys(key_id, key_secret):
    """Fail fast on malformed credentials before any network call."""
    if not str(key_id).startswith(("rzp_test_", "rzp_live_")):
        raise ValueError("key_id must start with rzp_test_ or rzp_live_")
    if not key_secret or len(str(key_secret)) < 8:
        raise ValueError("key_secret missing or too short")


def _default_transport(method, url):
    # url carries "|AUTH|<b64creds>" appended by _auth_url; stripped here so
    # the injectable transport contract stays a plain (method, url) callable.
    path_part, _, b64 = url.partition("|AUTH|")
    req = urllib.request.Request(
        path_part, method=method, headers={
            "Authorization": "Basic " + b64,
            "Accept": "application/json"})
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in RETRY_STATUSES and attempt < RETRIES:
                time.sleep(BACKOFF_S * (2 ** attempt))
                continue
            raise RazorpayAPIError(f"HTTP {e.code} from Razorpay", status=e.code)
        except urllib.error.URLError as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(BACKOFF_S * (2 ** attempt))
                continue
            raise RazorpayAPIError(f"network error: {e.reason}")
    raise RazorpayAPIError(f"unreachable after retries: {last_err}")


def _auth_url(key_id, key_secret, path):
    b64 = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    return f"{API_BASE}{path}|AUTH|{b64}"


def _get(key_id, key_secret, path, transport):
    url = _auth_url(key_id, key_secret, path)
    fn = transport or _transport_override or _default_transport
    status, data = fn("GET", url)
    if status != 200:
        raise RazorpayAPIError(f"unexpected status {status} for {path}", status=status)
    return data


def _epoch_date(ts):
    import datetime as _dt
    if isinstance(ts, (int, float)):
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).date().isoformat()
    return ""


def _paginate(key_id, key_secret, path, transport):
    core, _, query = path.partition("?")
    params = [p for p in query.split("&") if p] if query else []
    fixed = [p for p in params if not p.startswith("skip=")]
    skip = 0
    for _ in range(MAX_PAGES):
        data = _get(key_id, key_secret, f"{core}?{'&'.join(fixed + [f'skip={skip}'])}",
                    transport)
        items = (data or {}).get("items") or []
        yield items
        if len(items) < 100:
            return
        skip += 100


def fetch_settlements(key_id, key_secret, from_ts=None, to_ts=None, transport=None):
    """Canonical Source B rows across all pages (settled only)."""
    validate_keys(key_id, key_secret)
    params = ["count=100"]
    if from_ts:
        params.append(f"from={from_ts}")
    if to_ts:
        params.append(f"to={to_ts}")
    rows = []
    for items in _paginate(key_id, key_secret, f"/settlements?{'&'.join(params)}",
                           transport):
        for s in items:
            if str(s.get("status", "")).lower() not in SETTLED:
                continue
            sid = s.get("id", "")
            utr = str(s.get("utr") or "").replace(" ", "").upper()
            if not sid or not utr:
                continue
            rows.append({
                "source": "B", "external_id": sid,
                "settlement_id": sid.upper(), "utr": utr,
                "amount": int(s.get("amount", 0)),
                "merchant_id": "", "rail": "NEFT",
                "narration": f"razorpay api settlement {sid}",
                "txn_date": _epoch_date(s.get("created_at")),
            })
    return rows


def fetch_payments(key_id, key_secret, from_ts=None, to_ts=None, transport=None):
    """Canonical Source A rows (captured payments linked to a settlement).

    Payments without a settlement_id belong to future batches; including them
    would fabricate MISSING_IN_LEDGER noise, so they are skipped by design.
    """
    validate_keys(key_id, key_secret)
    params = ["count=100"]
    if from_ts:
        params.append(f"from={from_ts}")
    if to_ts:
        params.append(f"to={to_ts}")
    rows = []
    for items in _paginate(key_id, key_secret, f"/payments?{'&'.join(params)}",
                           transport):
        for p in items:
            if str(p.get("status", "")).lower() not in CAPTURED:
                continue
            sid = str(p.get("settlement_id") or "")
            pid = p.get("id", "")
            if not sid or not pid:
                continue
            notes = p.get("notes") or {}
            rows.append({
                "source": "A", "external_id": pid,
                "settlement_id": sid.upper(), "utr": "",
                "amount": int(p.get("amount", 0)),
                "merchant_id": "",
                "rail": str(notes.get("rail") or "UPI").upper(),
                "narration": f"razorpay api payment {pid} under {sid}",
                "txn_date": _epoch_date(p.get("created_at")),
            })
    return rows


def derive_bank_rows(settlement_b_rows, miss_rate=0.0, seed=7):
    """Declared Source-C derivation: one bank credit per settlement UTR.

    This models the bank leg honestly — Razorpay exposes the UTR it instructed
    the bank with; the actual bank statement remains an upload surface. With
    miss_rate=0 the derivation is identity; a seeded miss_rate exists ONLY for
    demo/eval scenarios and is always recorded in the batch source_label.
    """
    import random
    rng = random.Random(seed)
    rows, missed = [], 0
    for b in settlement_b_rows:
        if miss_rate and rng.random() < miss_rate:
            missed += 1
            continue
        rows.append({
            "source": "C", "external_id": f"BANK-{b['utr']}",
            "settlement_id": "", "utr": b["utr"],
            "amount": b["amount"], "merchant_id": "", "rail": "NEFT",
            "narration": f"NEFT CR {b['utr']} RAZORPAY SOFTWARE PVT LTD",
            "txn_date": b["txn_date"],
        })
    return rows, missed
