"""Cron-style batch scheduler.

Standard 5-field cron expressions (minute hour day-of-month month day-of-week,
UTC) drive recurring batch runs. Schedules persist in MongoDB; every app
instance runs an evaluation loop, but executions are guarded by lease-based
claims stored in Mongo, so concurrent instances never double-run a schedule.

Semantics:
  - Fields: *, lists (a,b), ranges (a-b), steps (*/n, a-b/n), month/dow names.
  - Day-of-month / day-of-week follow Vixie-cron: if BOTH are restricted the
    fire days are the UNION; if either is unrestricted it is an AND with time.
  - Missed runs are skipped (no backfill); next_run_at recomputes from "now"
    after every execution or manual trigger.
  - Claims carry claimed_by + claim_expires_at leases; startup recovery only
    clears expired leases so live jobs on other nodes are never stomped.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("recon.scheduler")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("dom", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 6),
)


class CronError(ValueError):
    pass


def _atom(expr, names, lo, hi):
    v = names.get(expr)
    if v is not None:
        return v
    try:
        n = int(expr)
    except ValueError:
        raise CronError(f"bad cron value {expr!r}")
    if not lo <= n <= hi and not (hi == 6 and n == 7):   # allow dow=7 == sunday
        raise CronError(f"cron value {n} out of range [{lo},{hi}]")
    return 0 if (hi == 6 and n == 7) else n


def _parse_field(expr, lo, hi, names=None):
    """Parse one cron field into a set of matching ints."""
    names = names or {}
    vals = set()
    for part in expr.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty cron field component in {expr!r}")
        step = 1
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"bad step {step_s!r}")
            if step < 1:
                raise CronError("cron step must be >= 1")
            start_end_hi = hi
        else:
            start_end_hi = None

        if body in ("*", ""):
            start, end = lo, hi
        elif "-" in body.lstrip("-"):
            a, b = body.split("-", 1)
            start, end = _atom(a, names, lo, hi), _atom(b, names, lo, hi)
            if start > end:
                raise CronError(f"inverted range {body!r}")
        else:
            start = _atom(body, names, lo, hi)
            end = start_end_hi if start_end_hi is not None else start

        vals.update(range(start, end + 1, step))
    for v in vals:
        if not lo <= v <= hi:
            raise CronError(f"cron value {v} out of range [{lo},{hi}]")
    return vals


def parse_cron(expr):
    """Parse a 5-field cron expression into per-field value sets."""
    parts = str(expr).split()
    if len(parts) != 5:
        raise CronError(f"cron must have exactly 5 fields, got {len(parts)}: {expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12, MONTHS)
    dow = _parse_field(parts[4], 0, 6, DOWS)
    return {
        "minute": minute, "hour": hour, "dom": dom, "month": month, "dow": dow,
        "dom_all": len(dom) == 31, "dow_all": len(dow) == 7,
    }


def next_fire(expr, after):
    """Earliest fire time strictly after `after` (UTC). Raises CronError if
    none exists within ~a year of minutes."""
    f = parse_cron(expr)
    t = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.month in f["month"]:
            # cron dow: Sun=0..Sat=6 == isoweekday() % 7
            cron_dow = t.isoweekday() % 7
            # Vixie rule: restricted dom AND restricted dow -> union of days
            if f["dom_all"]:
                day_ok = cron_dow in f["dow"]
            elif f["dow_all"]:
                day_ok = t.day in f["dom"]
            else:
                day_ok = t.day in f["dom"] or cron_dow in f["dow"]
            if day_ok and t.hour in f["hour"] and t.minute in f["minute"]:
                return t
        t += timedelta(minutes=1)
    raise CronError(f"no fire time within one year for {expr!r}")


def validate_cron(expr):
    parse_cron(expr)
    return True


# ------------------------------------------------------------------ service
class BatchScheduler:
    """Mongo-backed schedule store + asyncio evaluation loop.

    Multi-instance safety:
      - Every instance runs a tick loop, but execution is guarded by a LEASE:
        an atomic conditional claim sets {in_flight, claimed_by, claim_expires_at}.
        Losers observe the live lease and skip; no duplicates.
      - Leases expire (default 15 min) so a crashed instance cannot block its
        schedules forever. Startup recovery clears only EXPIRED leases — a
        restarting node never stomps a job running on another live node.

    Action runners are injected (`configure`) by the application to avoid a
    circular import with server.py; each runner receives
    (actor, trigger, schedule) kwargs and returns a summary dict.
    """

    CLAIM_LEASE_S = int(__import__("os").environ.get("SCHED_CLAIM_LEASE_S", "900"))

    def __init__(self, db, interval_s=None):
        import os
        self.db = db
        self.interval_s = interval_s or int(os.environ.get("SCHED_INTERVAL_S", "30"))
        self.instance_id = str(__import__("uuid").uuid4())
        self._runners = {}
        self._task = None

    def configure(self, runners):
        self._runners.update(runners)

    async def recover_leases(self):
        """Clear only leases that have already expired (crashed instances);
        live leases held by other nodes are respected."""
        cutoff = datetime.now(timezone.utc).isoformat()
        return await self.db.schedules.update_many(
            {"in_flight": True, "claim_expires_at": {"$lt": cutoff}},
            {"$set": {"in_flight": False},
             "$unset": {"claimed_by": "", "claim_expires_at": ""}})

    async def start(self):
        await self.recover_leases()
        self._task = asyncio.create_task(self._loop())
        logger.info("scheduler started on instance %s (%s)",
                    self.instance_id[:8], ", ".join(self._runners) or "no runners")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self.interval_s)

    async def tick(self):
        """Run every enabled schedule whose next_run_at has passed."""
        now = datetime.now(timezone.utc)
        ran = []
        docs = await self.db.schedules.find({"enabled": True}).to_list(200)
        for s in docs:
            nra = s.get("next_run_at")
            if not nra:
                continue
            try:
                due_dt = datetime.fromisoformat(nra)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                due = due_dt <= now
            except (ValueError, TypeError):
                logger.warning("schedule %s has unparsable next_run_at %r; skipping",
                               s.get("id"), nra)
                continue
            if not due:
                continue
            result = await self.run_now(s, triggered_by="schedule")
            if result is not None:                # skipped leases are not "runs"
                ran.append({"id": s["id"], "name": s["name"], "result": result})
        return ran

    async def run_now(self, schedule, triggered_by="manual", actor="scheduler@system"):
        """Execute one schedule under an atomic cross-instance lease.

        Manual triggers run even when `enabled` is false (explicit intent).
        Returns the runner result dict, or None when another live instance
        currently holds the lease or already advanced this run.

        Correctness under stale reads: the claim atomically ADVANCES
        next_run_at, and scheduled claims additionally require the SERVER-SIDE
        next_run_at to still be due. An instance acting on a pre-update
        snapshot therefore cannot execute a run another instance just took —
        including sequentially, after the first holder released.
        """
        sid = schedule["id"]
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_expiry = (now + timedelta(seconds=self.CLAIM_LEASE_S)).isoformat()
        try:
            nxt_iso = next_fire(schedule["cron"], now).isoformat()
        except CronError:
            nxt_iso = None

        claim_filter = {"id": sid,
                        "$or": [{"in_flight": {"$ne": True}},
                                {"claim_expires_at": {"$lt": now_iso}}]}
        if triggered_by == "schedule":
            claim_filter["next_run_at"] = {"$lte": now_iso}
        claimed = await self.db.schedules.find_one_and_update(
            claim_filter,
            {"$set": {"in_flight": True,
                      "claimed_by": self.instance_id,
                      "claim_expires_at": lease_expiry,
                      "next_run_at": nxt_iso}})
        if not claimed:
            logger.warning("schedule %s lease held elsewhere; skipping", sid)
            return None

        started = now_iso
        status, detail, result = "failed", {}, None
        try:
            runner = self._runners.get(schedule.get("action"))
            if runner is None:
                raise RuntimeError(f"no runner registered for action {schedule.get('action')!r}")
            result = await runner(actor=actor, trigger=triggered_by, schedule=schedule)
            status = "ok"
            detail = {"batch_id": getattr(result, "get", lambda k: None)("id"),
                      "batch_name": getattr(result, "get", lambda k: None)("name")}
        except Exception as e:  # noqa: BLE001
            detail = {"error": str(e)[:300]}
            logger.exception("schedule %s run failed", sid)
        finally:
            await self.db.schedules.update_one(
                {"id": sid, "claimed_by": self.instance_id},
                {"$set": {"last_run_at": started,
                          "last_status": status, "last_result": detail,
                          "last_triggered_by": triggered_by},
                 "$inc": {"run_count": 1},
                 "$unset": {"in_flight": "", "claimed_by": "", "claim_expires_at": ""}})
        return result or {"status": status, **detail}
