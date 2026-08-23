"""Multi-instance coordination guarantees.

These tests simulate what happens when several app instances share one
MongoDB: concurrent hash-chain appends must never fork, the rate-limit budget
must be shared across limiter instances, and scheduler leases must not be
stomped by restarting nodes.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from server import MongoFixedWindowLimiter, append_audit_events, GENESIS_HASH

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(MONGO_URL)[DB_NAME]


# ---------------------------------------------------------------- audit chain
class TestConcurrentAuditChain:
    def test_parallel_writers_never_fork_the_chain(self):
        """N 'instances' appending concurrently -> one linear chain, no gaps."""
        async def scenario():
            db = _db()
            before = await db.audit_events.count_documents({})

            async def writer(wid):
                specs = [{"batch_id": None, "actor": f"inst{wid}", "role": "admin",
                          "action": f"concurrent_probe_{wid}", "entity": "test",
                          "entity_id": f"{wid}-{i}", "details": {"w": wid}}
                         for i in range(5)]
                return await append_audit_events(specs, database=db)

            results = await asyncio.gather(*(writer(i) for i in range(8)))
            total = await db.audit_events.count_documents({})
            docs = await db.audit_events.find(
                {}, {"_id": 0}).sort("seq", 1).to_list(total + 1)
            return results, before, total, docs

        results, before, total, docs = asyncio.run(scenario())
        assert total == before + 40                       # every event persisted exactly once

        # strict sequential integrity across ALL events (the /audit/verify logic)
        prev_hash, expected_seq = GENESIS_HASH, 1
        for ev in docs:
            assert ev["prev_hash"] == prev_hash, f"fork at seq {ev['seq']}"
            assert ev["seq"] == expected_seq, f"gap at seq {ev['seq']}"
            import json, hashlib
            payload = {k: ev[k] for k in ("id", "batch_id", "actor", "role", "action",
                                          "entity", "entity_id", "details", "created_at")}
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            assert ev["hash"] == hashlib.sha256((prev_hash + canonical).encode()).hexdigest()
            prev_hash, expected_seq = ev["hash"], ev["seq"] + 1

    def test_single_append_still_returns_chained_event(self):
        async def scenario():
            return await append_audit_events([
                {"batch_id": None, "actor": "solo", "role": "admin",
                 "action": "solo_probe", "entity": "test", "entity_id": "x",
                 "details": {}}], database=_db())

        ev = asyncio.run(scenario())[0]
        assert ev["seq"] > 0 and len(ev["hash"]) == 64 and len(ev["prev_hash"]) == 64


# ---------------------------------------------------------------- rate limiting
class TestSharedRateLimit:
    def test_budget_shared_across_instances(self):
        """Two limiter instances (simulating two app nodes) share one budget."""
        async def scenario():
            db = _db()
            key = f"shared-test-{os.urandom(4).hex()}"
            a, b = MongoFixedWindowLimiter(db), MongoFixedWindowLimiter(db)
            outcomes = []
            for i in range(12):
                lim = a if i % 2 == 0 else b          # alternate between instances
                outcomes.append(await lim.allow(key, max_events=10, window_seconds=60))
            doc = await db.rate_limits.find_one({"_id": key})
            await db.rate_limits.delete_one({"_id": key})
            return outcomes, doc

        outcomes, doc = asyncio.run(scenario())
        assert outcomes[:10] == [True] * 10           # combined budget honoured
        assert outcomes[10:] == [False, False]        # both instances see exhaustion
        assert doc["count"] == 12                     # single shared bucket

    def test_window_reset_allows_again(self):
        async def scenario():
            db = _db()
            key = f"window-reset-{os.urandom(4).hex()}"
            lim = MongoFixedWindowLimiter(db)
            for _ in range(3):
                await lim.allow(key, max_events=3, window_seconds=60)
            exhausted = await lim.allow(key, max_events=3, window_seconds=60)
            # force the window into the past, as if it had elapsed
            await db.rate_limits.update_one(
                {"_id": key}, {"$set": {"window_end": int(datetime.now(timezone.utc).timestamp()) - 1}})
            again = await lim.allow(key, max_events=3, window_seconds=60)
            await db.rate_limits.delete_one({"_id": key})
            return exhausted, again

        exhausted, again = asyncio.run(scenario())
        assert exhausted is False and again is True


# ---------------------------------------------------------------- scheduler leases
class TestSchedulerLeases:
    def _svc(self):
        from scheduler import BatchScheduler
        return BatchScheduler(_db())

    def test_live_lease_blocks_other_instance(self):
        async def scenario():
            db = _db()
            sid = "lease-live"
            await db.schedules.delete_one({"id": sid})
            future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            await db.schedules.insert_one({
                "id": sid, "name": "held", "cron": "* * * * *",
                "action": "sandbox_seed", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "in_flight": True, "claimed_by": "other-instance",
                "claim_expires_at": future, "run_count": 0})
            svc = self._svc()
            ran = await svc.tick()
            doc = await db.schedules.find_one({"id": sid})
            await db.schedules.delete_one({"id": sid})
            return ran, doc

        ran, doc = asyncio.run(scenario())
        assert ran == []                                  # live lease respected
        assert doc["claimed_by"] == "other-instance"

    def test_expired_lease_is_recovered_and_rerun(self):
        async def scenario():
            db = _db()
            sid = "lease-expired"
            await db.schedules.delete_one({"id": sid})
            past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            await db.schedules.insert_one({
                "id": sid, "name": "crashed", "cron": "* * * * *",
                "action": "sandbox_seed", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "in_flight": True, "claimed_by": "dead-instance",
                "claim_expires_at": past, "run_count": 0})

            svc = self._svc()

            async def runner(actor="t", trigger="schedule", schedule=None):
                return {"id": "recovered-batch"}

            svc.configure({"sandbox_seed": runner})
            # Deterministic recovery path (no background loop): the loop is
            # spawned by start() and races an explicit tick() for the atomic
            # claim — exactly-once means only one of the two observes the run.
            await svc.recover_leases()
            try:
                ran = await asyncio.wait_for(svc.tick(), timeout=10)
            finally:
                await svc.stop()
            doc = await db.schedules.find_one({"id": sid}, {"_id": 0})
            await db.schedules.delete_one({"id": sid})
            return ran, doc

        ran, doc = asyncio.run(scenario())
        assert any(r["id"] == "lease-expired" for r in ran)
        assert doc["last_status"] == "ok"
        assert "claimed_by" not in doc and "in_flight" not in doc   # lease released

    def test_concurrent_ticks_execute_exactly_once(self):
        """Two 'instances' ticking at the same moment -> one execution total."""
        async def scenario():
            db = _db()
            sid = "lease-race"
            await db.schedules.delete_one({"id": sid})
            await db.schedules.insert_one({
                "id": sid, "name": "raced", "cron": "* * * * *",
                "action": "sandbox_seed", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "run_count": 0})
            runs = []

            def make_svc():
                from scheduler import BatchScheduler
                s = BatchScheduler(_db())

                async def runner(actor="t", trigger="schedule", schedule=None):
                    runs.append(s.instance_id)
                    return {"id": "race-batch"}

                s.configure({"sandbox_seed": runner})
                return s

            a, b = make_svc(), make_svc()          # distinct instance ids
            results = await asyncio.gather(a.tick(), b.tick())
            doc = await db.schedules.find_one({"id": sid}, {"_id": 0})
            await db.schedules.delete_one({"id": sid})
            return results, runs, doc

        results, runs, doc = asyncio.run(scenario())
        assert len(runs) == 1, f"exactly-once violated: {len(runs)} executions"
        executed = [r for r in results if r]
        assert len(executed) == 1
        assert any(item["id"] == "lease-race" for r in executed for item in r)
        assert doc["run_count"] == 1

    def test_startup_does_not_clear_live_leases_of_others(self):
        async def scenario():
            db = _db()
            sid = "lease-survive-restart"
            future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            await db.schedules.delete_one({"id": sid})
            await db.schedules.insert_one({
                "id": sid, "name": "long-job", "cron": "* * * * *",
                "action": "sandbox_seed", "enabled": True,
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "in_flight": True, "claimed_by": "node-a-running-long-job",
                "claim_expires_at": future, "run_count": 0})
            await self._svc().start()                      # simulates node B booting
            doc = await db.schedules.find_one({"id": sid})
            await db.schedules.delete_one({"id": sid})
            return doc

        doc = asyncio.run(scenario())
        assert doc["in_flight"] is True                    # untouched by the restart
        assert doc["claimed_by"] == "node-a-running-long-job"
