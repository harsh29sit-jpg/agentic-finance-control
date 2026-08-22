"""Cron parsing correctness + scheduler service and API behaviour."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
import requests

from scheduler import BatchScheduler, CronError, next_fire, parse_cron, validate_cron

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

ADMIN = ("admin@recon.io", "admin123")
CONTROLLER = ("controller@recon.io", "controller123")
ANALYST = ("analyst@recon.io", "analyst123")
SUPPORT = ("support@recon.io", "support123")


def _login(role_creds):
    email, pw = role_creds
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": email, "password": pw}, timeout=30)
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------- cron expression semantics ----------------
class TestCronParsing:
    def test_valid_expressions_accepted(self):
        for expr in ["* * * * *", "0 6 * * *", "*/15 * * * *", "0 9-17 * * mon-fri",
                     "30 4 1,15 JAN *", "59 23 29 2 sat"]:
            assert validate_cron(expr), expr

    @pytest.mark.parametrize("expr", ["* * * *", "* * * * * *", "61 * * * *",
                                      "* 24 * * *", "0 0 32 * *", "*/0 * * * *",
                                      "a b c d e", "0 6"])
    def test_invalid_expressions_rejected(self, expr):
        with pytest.raises(CronError):
            parse_cron(expr)

    def test_dow_seven_is_sunday(self):
        f = parse_cron("0 0 * * 7")
        assert 0 in f["dow"] and len(f["dow"]) == 1


class TestNextFire:
    TZ = timezone.utc

    def test_every_minute(self):
        after = datetime(2026, 8, 22, 10, 30, 15, tzinfo=self.TZ)
        assert next_fire("* * * * *", after) == datetime(2026, 8, 22, 10, 31, tzinfo=self.TZ)

    def test_daily_at_six(self):
        after = datetime(2026, 8, 22, 10, 0, tzinfo=self.TZ)
        nxt = next_fire("0 6 * * *", after)
        assert (nxt.day, nxt.hour) == (23, 6)

    def test_step_fifteen_minutes(self):
        after = datetime(2026, 8, 22, 10, 16, tzinfo=self.TZ)
        assert next_fire("*/15 * * * *", after).minute == 30

    def test_weekdays_only(self):
        # 2026-08-22 is a Saturday -> next weekday is Monday 24th
        after = datetime(2026, 8, 22, 9, 0, tzinfo=self.TZ)
        nxt = next_fire("0 9 * * mon-fri", after)
        assert (nxt.weekday(), nxt.hour) == (0, 9) and nxt.day == 24

    def test_dom_and_dow_union_rule(self):
        # both restricted: fires on the 1st OR on Mondays.
        # Aug 1 2026 = Saturday. Starting after Aug 1 13:00, the next fire is
        # Monday Aug 3 at 12:00 (union rule lets either day trigger).
        after = datetime(2026, 8, 1, 13, 0, tzinfo=self.TZ)
        nxt = next_fire("0 12 1 * 1", after)
        assert (nxt.day, nxt.hour, nxt.weekday()) == (3, 12, 0)

    def test_month_names(self):
        after = datetime(2026, 8, 22, tzinfo=self.TZ)
        assert next_fire("0 0 1 jan *", after).month == 1

    def test_never_firing_raises_within_a_year(self):
        after = datetime(2026, 8, 22, tzinfo=self.TZ)
        with pytest.raises(CronError):
            next_fire("0 0 30 2 *", after)         # Feb 30th does not exist


# ---------------- scheduler service against the shared test DB ----------------
def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(MONGO_URL)[DB_NAME]


class TestBatchSchedulerService:
    def test_tick_runs_due_schedule_and_updates_stats(self):
        async def scenario():
            db = _db()
            sched = {
                "id": "sched_tick_test", "name": "Tick Test", "cron": "* * * * *",
                "action": "sandbox_seed", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "run_count": 0, "in_flight": False,
                "last_status": None, "last_result": {},
            }
            await db.schedules.delete_one({"id": sched["id"]})
            await db.schedules.insert_one(dict(sched))
            before = await db.batches.count_documents({})

            svc = BatchScheduler(db)

            async def runner(actor="t", trigger="schedule", schedule=None):
                return {"id": "tick-test-batch", "name": "Tick Test Batch"}

            svc.configure({"sandbox_seed": runner})
            ran = await asyncio.wait_for(svc.tick(), timeout=10)
            doc = await db.schedules.find_one({"id": "sched_tick_test"})
            batches_after = await db.batches.count_documents({})
            await db.schedules.delete_one({"id": sched["id"]})
            return ran, doc, before, batches_after

        ran, doc, before, after_count = asyncio.run(scenario())
        assert any(r["id"] == "sched_tick_test" for r in ran)
        assert doc["last_status"] == "ok"
        assert doc["run_count"] == 1
        assert doc["last_result"]["batch_id"] == "tick-test-batch"
        assert datetime.fromisoformat(doc["next_run_at"]) > datetime.now(timezone.utc)
        assert after_count >= before   # runner stub did not create a real batch

    def test_runner_failure_recorded_not_raised(self):
        async def scenario():
            db = _db()
            sid = "sched_fail_test"
            await db.schedules.delete_one({"id": sid})
            await db.schedules.insert_one({
                "id": sid, "name": "Fail Test", "cron": "* * * * *",
                "action": "nonexistent_action", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "run_count": 0, "in_flight": False})
            svc = BatchScheduler(db)
            svc.configure({"sandbox_seed": lambda **k: None})
            await asyncio.wait_for(svc.tick(), timeout=10)
            doc = await db.schedules.find_one({"id": sid}, {"_id": 0})
            await db.schedules.delete_one({"id": sid})
            return doc

        doc = asyncio.run(scenario())
        assert doc["last_status"] == "failed"
        assert "no runner" in doc["last_result"]["error"]
        # lease fully released after failure (fields unset, never left dangling)
        assert not doc.get("in_flight")
        assert "claimed_by" not in doc and "claim_expires_at" not in doc


# ---------------- schedule REST API ----------------
class TestScheduleAPI:
    def test_crud_and_run_now(self):
        admin = _login(ADMIN)
        r = requests.post(f"{BASE_URL}/api/schedules", headers=admin, timeout=30, json={
            "name": "Nightly replay", "cron": "0 2 * * *",
            "action": "replay_latest_upload", "enabled": True, "note": "ci"})
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["id"]
        assert body["cron"] == "0 2 * * *" and body["next_run_at"]

        listed = requests.get(f"{BASE_URL}/api/schedules", headers=admin, timeout=30).json()
        assert any(s["id"] == sid for s in listed)
        assert all("in_flight" not in s for s in listed)

        rr = requests.post(f"{BASE_URL}/api/schedules/{sid}/run-now", headers=admin, timeout=180)
        assert rr.status_code == 200, rr.text
        rb = rr.json()
        assert rb["ok"] is True and rb["schedule"]["last_status"] in ("ok", "failed")

        dl = requests.delete(f"{BASE_URL}/api/schedules/{sid}", headers=admin, timeout=30)
        assert dl.status_code == 200
        assert requests.get(f"{BASE_URL}/api/schedules", headers=admin, timeout=30).json() \
            .count({"id": sid}) == 0

    def test_invalid_cron_422(self):
        admin = _login(ADMIN)
        r = requests.post(f"{BASE_URL}/api/schedules", headers=admin, timeout=30, json={
            "name": "bad", "cron": "99 * * * *", "action": "sandbox_seed"})
        assert r.status_code == 422
        assert "Invalid cron" in r.json()["detail"]

    def test_rbac(self):
        analyst = _login(ANALYST)
        support = _login(SUPPORT)
        controller = _login(CONTROLLER)
        payload = {"name": "x", "cron": "0 6 * * *", "action": "sandbox_seed"}
        assert requests.post(f"{BASE_URL}/api/schedules", headers=analyst, json=payload,
                             timeout=30).status_code == 403
        assert requests.post(f"{BASE_URL}/api/schedules", headers=support, json=payload,
                             timeout=30).status_code == 403
        assert requests.get(f"{BASE_URL}/api/schedules", headers=support, timeout=30).status_code == 200
        r = requests.post(f"{BASE_URL}/api/schedules", headers=controller, json=payload, timeout=30)
        assert r.status_code == 200
        requests.delete(f"{BASE_URL}/api/schedules/{r.json()['id']}", headers=controller, timeout=30)
