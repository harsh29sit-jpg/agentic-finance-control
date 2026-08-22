"""Razorpay dashboard-export connector.

Parses the CSV/XLSX reports merchants download from the Razorpay Dashboard
(Settlements and Payments reports) into canonical Source B / Source A rows.

Design notes:
  - Report type is detected from header signature, not file name.
  - Header row is located by scanning (some exports carry preamble rows).
  - Column names are matched through alias tables — Razorpay has renamed
    columns across dashboard versions ("created_at" vs "Settlement Date").
  - Amounts flow through adapters.parse_money, so rupee decimals with
    Indian grouping parse losslessly to paise.
  - Rows that cannot be mapped are skipped and *counted*, never dropped
    silently; unknown formats raise ConnectorError with the missing columns
    named so operators can self-serve.
"""
import csv
import io
import re

from adapters import parse_money, parse_date_any


class ConnectorError(ValueError):
    """Raised when a file does not look like the expected report format."""


# ---------------------------------------------------------------- aliases
SETTLEMENT_ALIASES = {
    "id": ["settlement_id", "settlement id", "settlementid", "id"],
    "utr": ["utr", "utr_no", "utr no", "bank_reference", "bank reference", "bank_ref_no"],
    "amount": ["amount", "net_amount", "net amount", "settled_amount", "credit"],
    "date": ["settlement_date", "settlement date", "created_at", "date",
             "settlement_utr date"],
    "fees": ["fees", "fee", "total_fees"],
    "tax": ["tax", "gst", "service_tax"],
    "status": ["status"],
}
PAYMENT_ALIASES = {
    "id": ["payment_id", "payment id", "paymentid", "id"],
    "settlement_id": ["settlement_id", "settlement id", "settlementid"],
    "amount": ["amount", "captured_amount", "amount_captured"],
    "date": ["payment_date", "created_at", "date", "captured_at"],
    "status": ["status"],
}
_SETTLED_OK = {"processed", "paid", "settled", "success", ""}


def _find_alias(headers_norm):
    """Map each logical field to its actual column name via alias tables.
    Alias candidates are normalized identically to headers before matching."""
    def build(aliases):
        out = {}
        for key, names in aliases.items():
            for n in names:
                n_norm = _norm_headers([n])[0]
                if n_norm in headers_norm:
                    out[key] = n
                    break
        return out
    return build


def _norm_headers(headers):
    return [re.sub(r"[^a-z0-9]", " ", str(h).strip().lower()) for h in headers]


def detect_report(headers):
    """Classify a report by its header row. Returns 'settlements' | 'payments' | None."""
    norm = _norm_headers(headers)
    smap = {}
    pmap = {}
    for key, names in SETTLEMENT_ALIASES.items():
        for n in names:
            if n in norm:
                smap[key] = n
                break
    for key, names in PAYMENT_ALIASES.items():
        for n in names:
            if n in norm:
                pmap[key] = n
                break
    if smap.get("id") and smap.get("utr") and smap.get("amount"):
        return "settlements"
    if pmap.get("id") and pmap.get("amount"):
        return "payments"
    return None


def read_tabular(filename: str, content: bytes):
    """Read CSV or XLSX into list-of-dicts with raw string values.

    Scans past preamble rows to find the real header. Returns (rows, header_row_idx).
    Raises ConnectorError on unreadable/empty files.
    """
    lower = (filename or "").lower()
    try:
        if lower.endswith(".xlsx") or lower.endswith(".xls"):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
            raw = [list(df.columns)] + df.values.tolist()
            start = 0
        else:
            text = content.decode("utf-8-sig", errors="replace")
            samples = text[:4096]
            try:
                dialect = csv.Sniffer().sniff(samples, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(io.StringIO(text), dialect)
            raw = [r for r in reader if any(str(c).strip() for c in r)]
            start = None
            # locate a recognizable header within the first lines; generic
            # tabular files (bank statements etc.) fall back to row 0.
            for idx, r in enumerate(raw[:10]):
                if detect_report(r):
                    start = idx
                    break
            if start is None:
                if not raw:
                    raise ConnectorError("file contains no readable rows")
                start = 0
        rows = []
        headers = [str(h).strip() for h in raw[start]]
        for vals in raw[start + 1:]:
            if len(vals) < len(headers):
                vals = list(vals) + [""] * (len(headers) - len(vals))
            rows.append(dict(zip(headers, [str(v).strip() for v in vals])))
        return headers, rows
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConnectorError(f"unreadable report {filename!r}: {e.__class__.__name__}: {e}")


def _norm_headers(headers):
    return [re.sub(r"[^a-z0-9]", " ", str(h).strip().lower()) for h in headers]


def _unknown_format_error(sample_row):
    return (
        "Unrecognized report format. Expected a Razorpay Settlements or Payments "
        f"export. Sample header seen: {[str(c)[:20] for c in sample_row][:8]}. "
        "Required columns — settlements: settlement_id, utr, amount, date; "
        "payments: payment_id, amount.")


def parse_settlements(rows):
    """Map Razorpay settlements rows -> canonical Source B records.

    Returns (records, stats). Non-settled statuses are skipped and counted.
    """
    if not rows:
        return [], {"skipped": 0, "parsed": 0}
    amap = _find_alias(_norm_headers(list(rows[0].keys())))(SETTLEMENT_ALIASES)
    missing = [f for f in ("id", "utr", "amount", "date") if f not in amap]
    if missing:
        raise ConnectorError(f"Settlements report missing columns: {missing}")
    records, skipped = [], 0
    for r in rows:
        status = (r.get(amap.get("status"), "") or "").lower()
        if status and status not in _SETTLED_OK:
            skipped += 1
            continue
        try:
            amount = parse_money(r.get(amap["amount"]))
            date = parse_date_any(r.get(amap["date"]))
        except ValueError:
            skipped += 1
            continue
        sid = r.get(amap["id"], "")
        utr = re.sub(r"\s+", "", r.get(amap["utr"], "")).upper()
        if not sid or not utr:
            skipped += 1
            continue
        records.append({
            "source": "B", "external_id": sid, "settlement_id": sid.upper(),
            "utr": utr, "amount": amount, "merchant_id": "",
            "rail": "NEFT", "narration": f"razorpay settlement {sid}",
            "txn_date": date,
        })
    return records, {"skipped": skipped, "parsed": len(records)}


def parse_payments(rows):
    """Map Razorpay payments rows -> canonical Source A records.

    Only payments already linked to a settlement are ingested; unlinked ones
    belong to future batches and would otherwise fabricate MISSING_IN_LEDGER noise.
    """
    if not rows:
        return [], {"skipped": 0, "parsed": 0}
    amap = _find_alias(_norm_headers(list(rows[0].keys())))(PAYMENT_ALIASES)
    if not amap.get("id") or not amap.get("amount"):
        raise ConnectorError("Payments report missing columns: payment_id / amount")
    records, skipped = [], 0
    for r in rows:
        status = (r.get(amap.get("status"), "") or "").lower()
        if status and status not in ("captured", "success", "", "settled"):
            skipped += 1
            continue
        sid = r.get(amap.get("settlement_id"), "")
        if not sid:
            skipped += 1
            continue
        try:
            amount = parse_money(r.get(amap["amount"]))
            date = parse_date_any(r.get(amap["date"])) if amap.get("date") else ""
        except ValueError:
            skipped += 1
            continue
        pid = r.get(amap["id"], "")
        records.append({
            "source": "A", "external_id": pid, "settlement_id": sid.upper(),
            "utr": "", "amount": amount, "merchant_id": "",
            "rail": "UPI", "narration": f"razorpay payment {pid} under {sid}",
            "txn_date": date,
        })
    return records, {"skipped": skipped, "parsed": len(records)}
