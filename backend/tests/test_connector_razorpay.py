"""Tests for the Razorpay dashboard-export connector (unit + live API)."""
import io

import pytest
import requests

from connectors import razorpay as rz
from connectors.razorpay import ConnectorError, detect_report, parse_payments, parse_settlements

BASE_URL = BASE = None  # set at module import below like other suites
import os  # noqa: E402

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

USERS = {"analyst": ("analyst@recon.io", "analyst123"),
         "support": ("support@recon.io", "support123")}


def _login(role):
    email, pw = USERS[role]
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": pw}, timeout=30)
    return r.json()["token"]


SETTLEMENTS_CSV = """settlement_id,created_at,status,fees,tax,amount,utr,currency
setl_ABC123,2026-06-01,processed,120.50,21.69,9980.00,HDFC2616712345678,INR
setl_DEF456,2026-06-02,processed,80.00,14.40,"₹12,450.75",ICIC260811234567,INR
setl_PEND789,2026-06-03,pending,0,0,500.00,SBIN2270012345678,INR
setl_BAD001,,processed,0,0,oops,,INR
"""

PAYMENTS_CSV = """payment_id,created_at,amount,status,settlement_id
pay_001,2026-05-31,3000.25,captured,setl_ABC123
pay_002,2026-05-31,2500.00,captured,setl_ABC123
pay_003,2026-06-01,999.00,captured,
pay_ref,2026-06-01,100.00,failed,setl_ABC123
"""

STATEMENT_CSV = """utr,amount,date,narration
HDFC2616712345678,9980.00,2026-06-02,NEFT CR HDFC2616712345678 RAZORPAY SETTLEMENT
UTR_ORPHAN_0001,777.77,2026-06-03,IMPS CR random credit no settlement
"""


# ---------------- unit: detection & parsing ----------------
class TestDetectReport:
    def test_settlements_headers(self):
        assert detect_report(["settlement_id", "created_at", "amount", "utr"]) == "settlements"

    def test_pretty_headers(self):
        assert detect_report(["Settlement Id", "Settlement Date", "Amount", "UTR No",
                              "Status"]) == "settlements"

    def test_payments_headers(self):
        assert detect_report(["payment_id", "amount", "status", "settlement_id"]) == "payments"

    def test_unknown(self):
        assert detect_report(["foo", "bar", "baz"]) is None


class TestParseSettlements:
    def test_aliases_amounts_and_skips(self):
        rows = list(__import__("csv").DictReader(io.StringIO(SETTLEMENTS_CSV)))
        recs, stats = parse_settlements(rows)
        by_id = {r["external_id"]: r for r in recs}
        assert set(by_id) == {"setl_ABC123", "setl_DEF456"}      # pending + garbage skipped
        assert stats == {"skipped": 2, "parsed": 2}
        assert by_id["setl_ABC123"]["amount"] == 998000          # rupees -> paise
        assert by_id["setl_ABC123"]["utr"] == "HDFC2616712345678"
        # Indian grouping with currency symbol parsed losslessly
        assert by_id["setl_DEF456"]["amount"] == 1245075
        assert all(r["source"] == "B" and r["rail"] == "NEFT" for r in recs)

    def test_missing_required_columns_raise_helpful_error(self):
        with pytest.raises(ConnectorError) as e:
            parse_settlements([{"id": "x"}])
        assert "utr" in str(e.value)


class TestParsePayments:
    def test_only_settled_linked_payments_ingested(self):
        rows = list(__import__("csv").DictReader(io.StringIO(PAYMENTS_CSV)))
        recs, stats = parse_payments(rows)
        assert [r["external_id"] for r in recs] == ["pay_001", "pay_002"]
        assert stats["skipped"] == 2                              # unlinked + failed
        assert all(r["source"] == "A" for r in recs)
        assert recs[0]["amount"] == 300025


# ---------------- integration: live API ----------------
class TestIngestionEndpoint:
    @pytest.fixture(scope="class")
    def token(self):
        return _login("analyst")

    def _files(self):
        return {
            "name": (None, "Connector Smoke Batch"),
            "settlements_file": ("settlements.csv", SETTLEMENTS_CSV.encode(), "text/csv"),
            "payments_file": ("payments.csv", PAYMENTS_CSV.encode(), "text/csv"),
            "statement_file": ("statement.csv", STATEMENT_CSV.encode(), "text/csv"),
        }

    def test_full_connector_flow(self, token):
        r = requests.post(f"{BASE_URL}/api/ingestion/razorpay",
                          headers={"Authorization": f"Bearer {token}"},
                          files=self._files(), timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("deduplicated") in (False, None)
        counts = body["counts"]
        assert counts["B"] == 2 and counts["A"] == 2 and counts["C"] == 2
        assert body["parse_summary"]["payments"]["skipped"] == 2
        bid = body["id"]

        # settlement matched bank credit exactly; orphan credit flagged
        rec = requests.get(f"{BASE_URL}/api/reconciliation?batch_id={bid}",
                           headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        assert any(m["status"] == "matched" and m["utr"] == "HDFC2616712345678"
                   and m["payments_count"] == 2 for m in rec)
        excs = requests.get(f"{BASE_URL}/api/exceptions?batch_id={bid}",
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        taxonomies = {e["taxonomy"] for e in excs["items"]}
        assert "UNIDENTIFIED_CREDIT" in taxonomies       # orphan statement credit
        assert "MISSING_IN_BANK" in taxonomies           # settlement without bank row

    def test_idempotent_reupload(self, token):
        r1 = requests.post(f"{BASE_URL}/api/ingestion/razorpay",
                           headers={"Authorization": f"Bearer {token}"},
                           files={**self._files(), "name": (None, "Second Name")},
                           timeout=120)
        assert r1.status_code == 200
        assert r1.json().get("deduplicated") is True

    def test_wrong_report_type_rejected_with_guidance(self, token):
        files = {
            "name": (None, "Bad"),
            "settlements_file": ("settlements.csv", PAYMENTS_CSV.encode(), "text/csv"),
        }
        r = requests.post(f"{BASE_URL}/api/ingestion/razorpay",
                          headers={"Authorization": f"Bearer {token}"},
                          files=files, timeout=60)
        assert r.status_code == 422
        assert "Settlements export" in r.json()["detail"]

    def test_support_role_forbidden(self):
        tok = _login("support")
        r = requests.post(f"{BASE_URL}/api/ingestion/razorpay",
                          headers={"Authorization": f"Bearer {tok}"},
                          files=self._files(), timeout=60)
        assert r.status_code == 403
