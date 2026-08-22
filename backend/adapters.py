"""Real-world ingestion adapters.

Bridges messy, real-world financial exports (bank statements, payment gateway
logs, mobile-money ledgers) into the canonical reconciliation model.

Design rules:
  - Money is parsed to integer paise at the boundary; nothing downstream ever
    sees floats or formatted strings.
  - Parsing never throws on bad data: unparseable values raise ValueError so
    the engine can isolate the row as invalid (no silent drops).
  - Bank-specific narration conventions (HDFC '/', ICICI '-', SBI prose,
    IMPS RRNs, UPI refs) are normalized here, once.
"""
import re
from datetime import datetime

# ---------------------------------------------------------------- money
_CURRENCY_TOKENS = re.compile(
    r"(?:^|\s)(?:INR|Rs\.?|EUR|USD|GBP|€|£|\$)(?=\s|$)", re.IGNORECASE)
_LEADING_SYMBOL = re.compile(r"^\s*(?:₹|€|£|\$)\s*")
_PARENS_NEG = re.compile(r"^\((.*)\)$")
_DR_CR_SUFFIX = re.compile(r"\s*(?:Dr|Cr)\.?$", re.IGNORECASE)
_UNICODE_MINUS = {"\u2212": "-", "\u2013": "-", "\u2014": "-"}
_NACODES = str.maketrans(_UNICODE_MINUS)


def parse_money(value) -> int:
    """Parse any human-formatted money string to integer paise.

    Handles: '₹485.00', '₹24,500.00' (Indian grouping), '-337,49',
    '+5257,28', '337,49 EUR' (European comma-decimals), '(123.45)'
    accounting negatives, '100 Dr', unicode minus, bare ints/floats,
    empty -> 0. Raises ValueError for garbage or negative amounts —
    callers isolate the row as invalid (debits are filtered upstream).
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"bool is not money: {value!r}")
    if isinstance(value, (int, float)):
        sign = -1 if value < 0 else 1
        mag = abs(value)
        if isinstance(value, float):
            if mag != mag or mag == float("inf"):
                raise ValueError(f"non-finite amount: {value!r}")
            mag = round(mag * 100)
        total = sign * int(mag)
    else:
        s = str(value).translate(_NACODES).strip()
        if not s:
            return 0

        negative_parens = False
        m = _PARENS_NEG.match(s)
        if m:
            negative_parens = True
            s = m.group(1)

        s = _LEADING_SYMBOL.sub("", s)
        s = _CURRENCY_TOKENS.sub(" ", s).replace("€", "").replace("£", "").replace("$", "")
        s = _DR_CR_SUFFIX.sub("", s).strip()
        # drop any trailing currency code left glued without space
        s = re.sub(r"(?i)\s*(?:INR|EUR|USD|GBP)$", "", s).strip()

        sign = 1
        if s[:1] in ("-", "+"):
            sign = -1 if s[0] == "-" else 1
            s = s[1:]

        if not re.fullmatch(r"[0-9.,'\u00a0 ]+", s):
            raise ValueError(f"unparseable money: {value!r}")

        work = s.replace("'", "").replace("\u00a0", "").replace(" ", "")
        if "," in work and "." not in work:
            parts = work.split(",")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                whole, frac = parts                      # European decimal comma
                frac = (frac + "00")[:2]
                magnitude = int(whole.replace(".", "")) * 100 + int(frac)
            else:
                magnitude = int(work.replace(",", "")) * 100
        else:
            work = work.replace(",", "")
            if "." in work:
                whole, frac = work.rsplit(".", 1)
                frac = (frac + "00")[:2]
                magnitude = int(whole or "0") * 100 + int(frac)
            else:
                magnitude = int(work) * 100

        total = -magnitude if (sign < 0 or negative_parens) else magnitude

    if total < 0:
        raise ValueError(f"negative amounts are not valid ledger credits: {value!r}")
    return int(total)


# ---------------------------------------------------------------- dates
_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y",
)


def parse_date_any(value) -> str:
    """Normalize the date formats observed across real bank exports to ISO."""
    if not value:
        return ""
    s = str(value).strip()
    # Monzo-style '2018-02-25 12:34:56 +0000' -> keep date part via fromisoformat
    try:
        return datetime.fromisoformat(s.replace(" +0000", "")).date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {value!r}")


# ---------------------------------------------------------------- narrations / keys
_UTR_PATTERNS = (
    re.compile(r"(?:NEFT|RTGS)\s*[-/: ]?\s*(?:CR[:/ ])?\s*([A-Z]{4}[0-9A-Z]{10,18})", re.IGNORECASE),
    re.compile(r"\bUTR[:\s-]*([A-Z0-9]{10,22})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z]{4}[0-9]{2}[0-9]{3}[0-9]{7})\b"),          # RBI UTR structure
    re.compile(r"\bIMPS\s*[-/: ]\s*([0-9]{12})\b", re.IGNORECASE),   # IMPS RRN
)
_UPI_REF = re.compile(r"UPI[-/](?:P2M|P2P|COLLECT|MER)?[-/]([0-9]{9,12})", re.IGNORECASE)


def extract_reference(narration):
    """Pull the most credible match key out of a bank narration.

    Returns (kind, reference) where kind in {utr, rrn, upi, None}.
    """
    n = str(narration or "")
    for pat in _UTR_PATTERNS:
        m = pat.search(n)
        if m:
            return ("rrn" if "IMPS" in pat.pattern.upper() or len(m.group(1)) == 12
                    else "utr", m.group(1).upper())
    m = _UPI_REF.search(n)
    if m:
        return ("upi", m.group(1))
    return (None, None)


_RAIL_HINTS = (
    ("UPI", "UPI"), ("IMPS", "IMPS"), ("NEFT", "NEFT"), ("RTGS", "RTGS"),
    ("NACH", "NACH"), ("ACH", "NACH"), ("ECS", "NACH"), ("CHQ", "CHEQUE"),
)


def detect_rail(narration, default="OTHER"):
    n = str(narration or "").upper()
    for token, rail in _RAIL_HINTS:
        if token in n:
            return rail
    return default


def clean_merchant(narration):
    """Extract a merchant-ish token from narration text for grouping views."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9@.&]+", str(narration or ""))
              if t and not t.isdigit() and len(t) > 2]
    return " ".join(tokens[:3]).upper() if tokens else ""


# ---------------------------------------------------------------- canonical mapping
def canonical_row(source, external_id, settlement_id="", utr="", amount=0,
                  merchant_id="", rail="", narration="", txn_date=""):
    """Build one canonical raw row (as engine.normalize_record expects)."""
    kind, ref = ((None, utr) if utr else extract_reference(narration))
    return {
        "source": source,
        "external_id": external_id,
        "settlement_id": settlement_id,
        "utr": ref or "",
        "amount": parse_money(amount),
        "merchant_id": merchant_id or clean_merchant(narration)[:40],
        "rail": rail or detect_rail(narration),
        "narration": narration,
        "txn_date": parse_date_any(txn_date),
    }
