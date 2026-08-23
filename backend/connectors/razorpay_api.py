"""Razorpay public-API client for scheduled settlement pulls.

Amounts from the Razorpay API are already integer paise — mapped verbatim.
The transport is injectable (`transport(url) -> dict`) so tests and the
scheduler can run without network access.
"""
import base64
import json
import urllib.request

API_BASE = "https://api.razorpay.com/v1"
SETTLED = {"processed"}
# Test/scheduler injection point: set to a callable(url)->dict to bypass HTTP.
_transport_override = None


def _default_transport(url, key_id, key_secret):
    req = urllib.request.Request(
        url, headers={
            "Authorization": "Basic " + base64.b64encode(
                f"{key_id}:{key_secret}".encode()).decode(),
            "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return json.loads(resp.read())


def fetch_settlements(key_id, key_secret, from_ts=None, to_ts=None,
                      transport=None):
    """Yield canonical Source B rows across all pages.

    transport: callable(url)->dict; defaults to live HTTP.
    """
    transport = transport or _default_transport
    rows = []
    skip = 0
    while True:
        params = [f"count=100", f"skip={skip}"]
        if from_ts:
            params.append(f"from={from_ts}")
        if to_ts:
            params.append(f"to={to_ts}")
        data = transport(f"{API_BASE}/settlements?{'&'.join(params)}")
        items = (data or {}).get("items") or []
        if not items:
            break
        for s in items:
            if str(s.get("status", "")).lower() not in SETTLED:
                continue
            sid = s.get("id", "")
            utr = str(s.get("utr") or "").replace(" ", "").upper()
            if not sid or not utr:
                continue
            created = s.get("created_at")
            date = ""
            if isinstance(created, (int, float)):
                import datetime as _dt
                date = _dt.datetime.fromtimestamp(created, tz=_dt.timezone.utc) \
                    .date().isoformat()
            rows.append({
                "source": "B", "external_id": sid,
                "settlement_id": sid.upper(), "utr": utr,
                "amount": int(s.get("amount", 0)),
                "merchant_id": "", "rail": "NEFT",
                "narration": f"razorpay api settlement {sid}",
                "txn_date": date,
            })
        if len(items) < 100:
            break
        skip += 100
    return rows
