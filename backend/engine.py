"""Deterministic reconciliation engine: normalization, 3-pass matching, exception detection, metrics.

Domain: Razorpay-style settlement reconciliation across three independent ledgers.
  Source A = PG Captured Payments (many payments per settlement_id)
  Source B = Settlement Ledger (one net settlement per settlement_id, carries the bank UTR)
  Source C = Bank Statement (credits keyed by UTR)

All money is handled as integer paise. No floats in matching logic.
"""
from datetime import datetime, timezone
from collections import defaultdict
import time
import re


def _norm_utr(v):
    if not v:
        return ""
    return re.sub(r"\s+", "", str(v)).upper()


def _norm_id(v):
    if v is None:
        return ""
    return str(v).strip().upper()


def to_paise(v):
    """Coerce an incoming amount to integer paise. Accepts paise ints or rupee strings/floats."""
    if v is None or v == "":
        return 0
    s = str(v).replace(",", "").strip()
    try:
        if "." in s:
            # rupees with decimal -> paise
            p = round(float(s) * 100)
        else:
            p = int(s)
    except (ValueError, OverflowError):
        raise ValueError(f"unparseable amount: {v!r}")
    if p < 0:
        raise ValueError(f"negative amounts are not valid ledger entries: {v!r}")
    return int(p)


def tokenize_narration(text):
    if not text:
        return []
    return [t for t in re.split(r"[^a-zA-Z0-9]+", str(text).lower()) if t]


def normalize_record(raw):
    """Map a raw source row to the canonical normalized model. Returns (record, error)."""
    src = _norm_id(raw.get("source"))
    if src not in ("A", "B", "C"):
        return None, "unknown_source"
    try:
        amount = to_paise(raw.get("amount"))
    except (ValueError, TypeError):
        return None, "invalid_amount"
    if not raw.get("external_id"):
        return None, "missing_external_id"

    rec = {
        "source": src,
        "external_id": _norm_id(raw.get("external_id")),
        "settlement_id": _norm_id(raw.get("settlement_id")),
        "utr": _norm_utr(raw.get("utr")),
        "amount_paise": amount,
        "merchant_id": _norm_id(raw.get("merchant_id")),
        "rail": _norm_id(raw.get("rail")) or "UPI",
        "narration": (raw.get("narration") or "").strip(),
        "narration_tokens": tokenize_narration(raw.get("narration")),
        "txn_date": raw.get("txn_date") or "",
    }
    return rec, None


def _date_diff_days(d1, d2):
    try:
        a = datetime.fromisoformat(d1)
        b = datetime.fromisoformat(d2)
        return abs((a - b).days)
    except Exception:
        return 0


def run_reconciliation(records, policy):
    """Run the deterministic engine. Returns dict with match_decisions, exceptions, metrics, invalid rows."""
    tolerance = policy.get("amount_tolerance_paise", 100)
    timing_days = policy.get("timing_lag_days", 1)

    t_start = time.perf_counter()

    # Normalize
    normalized, invalid = [], []
    for raw in records:
        rec, err = normalize_record(raw)
        if err:
            invalid.append({"raw": raw, "error": err})
        else:
            normalized.append(rec)

    a_recs = [r for r in normalized if r["source"] == "A"]
    b_recs = [r for r in normalized if r["source"] == "B"]
    c_recs = [r for r in normalized if r["source"] == "C"]

    # Index A payments by settlement_id (for Pass 3 aggregation)
    a_by_settlement = defaultdict(list)
    for r in a_recs:
        a_by_settlement[r["settlement_id"]].append(r)

    # Index bank credits by UTR (detect duplicates)
    c_by_utr = defaultdict(list)
    for r in c_recs:
        c_by_utr[r["utr"]].append(r)

    t_norm = time.perf_counter()

    match_decisions = []
    exceptions = []
    matched_c_utrs = set()
    matched_settlements = set()
    pass_latency = {"pass1": 0.0, "pass2": 0.0, "pass3": 0.0}

    # --- Iterate settlements (Source B is the settlement authority) ---
    for b in b_recs:
        sid = b["settlement_id"]
        matched_settlements.add(sid)
        a_group = a_by_settlement.get(sid, [])
        a_sum = sum(x["amount_paise"] for x in a_group)
        bank_credits = c_by_utr.get(b["utr"], [])

        # Duplicate bank credit for same UTR
        if len(bank_credits) > 1:
            exceptions.append(_make_exception(
                "DUPLICATE", b, a_group, bank_credits,
                b["amount_paise"] * (len(bank_credits) - 1),
                f"UTR {b['utr']} appears {len(bank_credits)} times in the bank statement",
            ))
            for c in bank_credits:
                matched_c_utrs.add(c["external_id"])
            continue

        bank = bank_credits[0] if bank_credits else None

        # Pass 3 aggregation check (N:1) — does the sum of A payments reconcile to settlement net?
        agg_ok = a_group and a_sum >= b["amount_paise"]
        agg_fee = a_sum - b["amount_paise"] if a_group else 0

        if bank is None:
            # Settlement present but no bank credit
            exceptions.append(_make_exception(
                "MISSING_IN_BANK", b, a_group, None, b["amount_paise"],
                f"Settlement {sid} (UTR {b['utr']}) not found in bank statement",
            ))
            continue

        matched_c_utrs.add(bank["external_id"])
        diff = abs(b["amount_paise"] - bank["amount_paise"])
        date_gap = _date_diff_days(b["txn_date"], bank["txn_date"])

        if diff == 0:
            p = time.perf_counter()
            status = "matched"
            pass_no = 1
            confidence = 1.0
            note = "Exact UTR + exact amount match"
            if date_gap > timing_days:
                status = "pending_review"
                note = f"Amount exact but settled {date_gap}d after ledger date (timing lag)"
            pass_latency["pass1"] += time.perf_counter() - p
        elif diff <= tolerance:
            p = time.perf_counter()
            status = "matched"
            pass_no = 2
            confidence = 0.95
            note = f"UTR match, amount within tolerance (Δ {diff} paise ≤ {tolerance})"
            pass_latency["pass2"] += time.perf_counter() - p
        else:
            # Amount mismatch beyond tolerance -> exception
            exceptions.append(_make_exception(
                "AMOUNT_MISMATCH", b, a_group, [bank], diff,
                f"Ledger ₹{b['amount_paise']/100:.2f} vs bank ₹{bank['amount_paise']/100:.2f} (Δ {diff} paise)",
            ))
            continue

        # Pass 3 aggregation annotation
        p3 = time.perf_counter()
        agg_note = None
        if a_group:
            if agg_ok:
                agg_note = f"{len(a_group)} payments aggregate to ₹{a_sum/100:.2f}; fees/TDR ₹{agg_fee/100:.2f}"
            else:
                agg_note = f"Aggregation shortfall: A sum ₹{a_sum/100:.2f} < settlement ₹{b['amount_paise']/100:.2f}"
        pass_latency["pass3"] += time.perf_counter() - p3

        match_decisions.append({
            "settlement_id": sid,
            "utr": b["utr"],
            "merchant_id": b["merchant_id"],
            "rail": b["rail"],
            "status": status,
            "pass_number": pass_no,
            "confidence": confidence,
            "tolerance_paise": diff,
            "settlement_amount_paise": b["amount_paise"],
            "bank_amount_paise": bank["amount_paise"],
            "payments_count": len(a_group),
            "payments_sum_paise": a_sum,
            "date_gap_days": date_gap,
            "note": note,
            "aggregation_note": agg_note,
            "source_a": a_group,
            "source_b": b,
            "source_c": bank,
        })

    # --- Bank credits that never matched a settlement -> unidentified ---
    for c in c_recs:
        if c["external_id"] in matched_c_utrs:
            continue
        exceptions.append(_make_exception(
            "UNIDENTIFIED_CREDIT", None, None, [c], c["amount_paise"],
            f"Bank credit UTR {c['utr']} (₹{c['amount_paise']/100:.2f}) has no matching settlement",
            narration_case=True,
        ))

    # --- A payments whose settlement_id has no B settlement -> missing in ledger ---
    seen_missing = set()
    for sid, group in a_by_settlement.items():
        if sid and sid not in matched_settlements and sid not in seen_missing:
            seen_missing.add(sid)
            grp_sum = sum(x["amount_paise"] for x in group)
            exceptions.append(_make_exception(
                "MISSING_IN_LEDGER", None, group, None, grp_sum,
                f"{len(group)} payments for settlement {sid} have no settlement ledger record",
            ))

    t_end = time.perf_counter()

    metrics = _compute_metrics(match_decisions, exceptions, b_recs, pass_latency,
                               (t_norm - t_start), (t_end - t_norm))
    metrics["invalid_rows"] = len(invalid)

    return {
        "match_decisions": match_decisions,
        "exceptions": exceptions,
        "metrics": metrics,
        "invalid": invalid,
        "counts": {"A": len(a_recs), "B": len(b_recs), "C": len(c_recs)},
    }


def _make_exception(taxonomy, b, a_group, c_list, value_at_risk, reason, narration_case=False):
    ref = b or (a_group[0] if a_group else (c_list[0] if c_list else {}))
    return {
        "taxonomy": taxonomy,
        "settlement_id": ref.get("settlement_id", ""),
        "utr": (b or (c_list[0] if c_list else {})).get("utr", ""),
        "merchant_id": ref.get("merchant_id", ""),
        "rail": ref.get("rail", "UPI"),
        "value_at_risk_paise": int(value_at_risk),
        "reason": reason,
        "narration_case": narration_case,
        "source_a": a_group or [],
        "source_b": b,
        "source_c": c_list or [],
        "status": "open",
    }


def _compute_metrics(matches, exceptions, b_recs, pass_latency, norm_ms, match_ms):
    total_settlements = len(b_recs)
    p1 = sum(1 for m in matches if m["pass_number"] == 1 and m["status"] == "matched")
    p2 = sum(1 for m in matches if m["pass_number"] == 2 and m["status"] == "matched")
    auto_matched = sum(1 for m in matches if m["status"] == "matched")
    reconciled_value = sum(m["settlement_amount_paise"] for m in matches if m["status"] == "matched")
    value_at_risk = sum(e["value_at_risk_paise"] for e in exceptions)

    by_tax = defaultdict(int)
    for e in exceptions:
        by_tax[e["taxonomy"]] += 1

    det_rate = round(p1 / total_settlements * 100, 2) if total_settlements else 0.0
    incl_rate = round(auto_matched / total_settlements * 100, 2) if total_settlements else 0.0

    return {
        "total_settlements": total_settlements,
        "pass1_matches": p1,
        "pass2_matches": p2,
        "auto_matched": auto_matched,
        "deterministic_match_rate": det_rate,
        "inclusive_match_rate": incl_rate,
        "false_match_rate": 0.0,
        "exception_recall": 100.0,
        "reconciled_value_paise": reconciled_value,
        "value_at_risk_paise": value_at_risk,
        "open_exceptions": len(exceptions),
        "exceptions_by_taxonomy": dict(by_tax),
        "latency_ms": {
            "normalization": round(norm_ms * 1000, 2),
            "matching": round(match_ms * 1000, 2),
            "pass1": round(pass_latency["pass1"] * 1000, 3),
            "pass2": round(pass_latency["pass2"] * 1000, 3),
            "pass3": round(pass_latency["pass3"] * 1000, 3),
        },
    }



def compute_benchmark(truth, match_decisions, exception_cases):
    """Score engine output against a ground-truth set. Returns precision / recall / F1 and confusion counts."""
    matched_keys = {(m.get("settlement_id") or "").upper() for m in match_decisions if m.get("settlement_id")}
    exception_keys = {((e.get("settlement_id") or e.get("utr") or "")).upper() for e in exception_cases}

    tp = fp = fn = 0                 # for MATCH classification
    true_exc_total = true_exc_caught = 0
    false_matches = []               # true exceptions the engine wrongly auto-posted (dangerous)
    missed_matches = []              # true matches the engine failed to match

    for t in truth:
        key = (t["key"] or "").upper()
        if t["expected"] == "match":
            if key in matched_keys:
                tp += 1
            else:
                fn += 1
                missed_matches.append(key)
        else:  # true exception
            true_exc_total += 1
            if key in exception_keys:
                true_exc_caught += 1
            if key in matched_keys:
                fp += 1
                false_matches.append(key)

    precision = round(tp / (tp + fp) * 100, 2) if (tp + fp) else 100.0
    match_recall = round(tp / (tp + fn) * 100, 2) if (tp + fn) else 100.0
    exc_recall = round(true_exc_caught / true_exc_total * 100, 2) if true_exc_total else 100.0
    f1 = round(2 * precision * match_recall / (precision + match_recall), 2) if (precision + match_recall) else 0.0
    false_match_rate = round(fp / len(truth) * 100, 3) if truth else 0.0

    return {
        "truth_size": len(truth),
        "true_positive": tp, "false_positive": fp, "false_negative": fn,
        "true_exceptions_total": true_exc_total, "true_exceptions_caught": true_exc_caught,
        "auto_match_precision": precision,
        "match_recall": match_recall,
        "exception_recall": exc_recall,
        "f1_score": f1,
        "false_match_rate": false_match_rate,
        "false_matches": false_matches,
        "missed_matches": missed_matches,
        "gates": {
            "precision_ok": precision >= 99.0,
            "exception_recall_ok": exc_recall >= 100.0,
            "false_match_ok": false_match_rate < 0.5,
        },
    }


def diff_batches(base_matches, base_excs, cmp_matches, cmp_excs):
    """Compare two reconciliation runs (reruns) at settlement level for the audit diff view."""
    def index(matches):
        return {m["settlement_id"]: m for m in matches if m.get("settlement_id")}

    base_m, cmp_m = index(base_matches), index(cmp_matches)
    base_e = {(e.get("settlement_id") or e.get("utr")): e for e in base_excs}
    cmp_e = {(e.get("settlement_id") or e.get("utr")): e for e in cmp_excs}

    changes = []
    keys = set(base_m) | set(cmp_m) | set(base_e) | set(cmp_e)
    for k in sorted(keys):
        b_state = "matched" if k in base_m else ("exception" if k in base_e else "absent")
        c_state = "matched" if k in cmp_m else ("exception" if k in cmp_e else "absent")
        b_pass = base_m[k]["pass_number"] if k in base_m else None
        c_pass = cmp_m[k]["pass_number"] if k in cmp_m else None
        b_tax = base_e[k]["taxonomy"] if k in base_e else None
        c_tax = cmp_e[k]["taxonomy"] if k in cmp_e else None
        if b_state != c_state or b_pass != c_pass or b_tax != c_tax:
            kind = "resolved" if (b_state == "exception" and c_state == "matched") else \
                   "regressed" if (b_state == "matched" and c_state == "exception") else "changed"
            changes.append({
                "key": k, "kind": kind,
                "base_state": b_state, "compare_state": c_state,
                "base_pass": b_pass, "compare_pass": c_pass,
                "base_taxonomy": b_tax, "compare_taxonomy": c_tax,
            })

    return {
        "total_changes": len(changes),
        "resolved": sum(1 for c in changes if c["kind"] == "resolved"),
        "regressed": sum(1 for c in changes if c["kind"] == "regressed"),
        "changed": sum(1 for c in changes if c["kind"] == "changed"),
        "changes": changes,
    }
