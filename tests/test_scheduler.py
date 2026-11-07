import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.scheduler.runner import HEARTBEAT_ID
from kiro_crew.scheduler.spec import ScheduleError, describe, next_run, parse_duration, parse_when

from .conftest import make_config, wait_for

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()


@pytest.mark.parametrize("text, secs", [("30m", 1800), ("2 hours", 7200), ("1d", 86400), ("45 sec", 45), ("1w", 604800)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


def test_parse_when_forms():
    p = parse_when("in 30m", now=NOW)
    assert (p.kind, p.next_run) == ("at", NOW + 1800)
    p = parse_when("every 2h", now=NOW)
    assert (p.kind, p.expr, p.next_run) == ("every", "7200", NOW + 7200)
    p = parse_when("cron 0 9 * * 1-5", tz="Europe/Berlin", now=NOW)  # Fri 6 Nov 12:00 UTC -> next Mon 9:00 CET
    nxt = datetime.fromtimestamp(p.next_run, ZoneInfo("Europe/Berlin"))
    assert (p.kind, nxt.weekday(), nxt.hour, nxt.day) == ("cron", 0, 9, 9)
    assert parse_when("0 9 * * *", now=NOW).kind == "cron"  # bare cron accepted
    p = parse_when("at 2026-11-07 09:00", tz="Asia/Tokyo", now=NOW)
    assert datetime.fromtimestamp(p.next_run, ZoneInfo("Asia/Tokyo")).hour == 9


@pytest.mark.parametrize("bad", ["", "soon", "in banana", "every 10s", "at yesterday", "at 2020-01-01 00:00",
                                 "cron 99 * * * *", "* * *"])
def test_parse_when_rejects(bad):
    with pytest.raises(ScheduleError):
        parse_when(bad, now=NOW)


def test_next_run_and_describe():
    assert next_run("at", "1", "UTC", NOW) is None
    assert next_run("every", "60", "UTC", NOW) == NOW + 60
    assert describe("every", "7200") == "every 2h" and describe("cron", "0 9 * * *") == "cron 0 9 * * *"


def _mk(store, kind="every", expr="60", first=None, mode="message", **kw):
    return store.create(kind=kind, expr=expr, first_run=first if first is not None else time.time() - 1,
                        mode=mode, title="t", payload="p", agent=None, address=Address("fake", "c1"),
                        user_id=kw.pop("user_id", "u"), **kw)


def test_claim_leases_without_advancing_then_complete_advances(app):
    s = app.ctx.schedules
    one = _mk(s, "at", str(time.time() - 1))
    rec = _mk(s)
    future = _mk(s, first=time.time() + 3600)
    due = {x.id for x in s.claim_due("UTC")}
    assert due == {one.id, rec.id}
    # still leased, not advanced: a second claim this tick returns nothing, run_count unchanged
    assert s.claim_due("UTC") == []
    assert s.get(rec.id).run_count == 0 and s.get(rec.id).enabled is True
    # completing the firing advances each
    for sid in due:
        s.complete_firing(sid, "UTC")
    assert s.get(one.id).enabled is False  # one-shot finished
    assert s.get(rec.id).next_run > time.time() and s.get(rec.id).run_count == 1
    assert s.claim_due("UTC") == []  # nothing due now
    assert s.get(future.id).run_count == 0


def test_crash_before_completion_is_re_claimed_at_least_once(app):
    """A fired-but-not-completed schedule (process died) is re-claimed once its lease expires."""
    s = app.ctx.schedules
    job = _mk(s, "at", str(time.time() - 1))
    claimed = s.claim_due("UTC", lease_s=300)
    assert [x.id for x in claimed] == [job.id]
    # simulate a crash: lease held, next_run not advanced. A new tick within the lease must NOT re-claim.
    assert s.claim_due("UTC") == []
    # after the lease expires it is re-claimed (the run was not lost)
    future = time.time() + 400
    assert [x.id for x in s.claim_due("UTC", now=future)] == [job.id]
    s.complete_firing(job.id, "UTC", now=future)
    assert s.get(job.id).enabled is False


def test_release_firing_allows_immediate_retry(app):
    s = app.ctx.schedules
    job = _mk(s)
    s.claim_due("UTC")
    assert s.claim_due("UTC") == []  # leased
    s.release_firing(job.id)  # fire() raised -> lease dropped
    assert [x.id for x in s.claim_due("UTC")] == [job.id]  # retried on the next tick, no wait


def test_missed_recurring_job_fires_once_not_backlog(app):
    s = app.ctx.schedules
    rec = _mk(s, expr="60", first=time.time() - 3600)  # an hour of missed ticks
    claimed = s.claim_due("UTC")
    assert len(claimed) == 1
    s.complete_firing(rec.id, "UTC")
    assert s.get(rec.id).next_run > time.time()
    assert s.claim_due("UTC") == []  # resumes on cadence, no backlog storm


def test_list_scope_and_delete_ownership(app):
    s = app.ctx.schedules
    mine, theirs = _mk(s, user_id="me"), _mk(s, user_id="other")
    assert [x.id for x in s.list("me")] == [mine.id]
    assert not s.delete(theirs.id, user_id="me")  # cannot cancel someone else's
    assert s.delete(theirs.id)


async def test_message_schedule_is_delivered_by_running_scheduler(app, chan):
    _mk(app.ctx.schedules, "at", str(time.time() - 1), mode="message")
    app.scheduler.start()
    try:
        await wait_for(lambda: any("Reminder: p" in m for m in chan.out), timeout=5)
    finally:
        await app.scheduler.stop()


async def test_task_schedule_enqueues_and_notifies(app, chan):
    s = _mk(app.ctx.schedules, "at", str(time.time() - 1), mode="task")
    app.scheduler.start()
    try:
        await wait_for(lambda: any("done: t" in m for m in chan.out), timeout=8)
    finally:
        await app.scheduler.stop()
    sched = app.ctx.schedules.get(s.id)
    assert sched.enabled is False  # one-shot completed and advanced by the running loop


async def test_recurring_schedule_fires_repeatedly_via_running_loop(app, chan):
    s = _mk(app.ctx.schedules, "every", "60", first=time.time() - 1, mode="message")
    app.scheduler.start()
    try:
        await wait_for(lambda: app.ctx.schedules.get(s.id).run_count >= 1, timeout=5)
    finally:
        await app.scheduler.stop()
    row = app.ctx.schedules.get(s.id)
    assert row.enabled is True and row.next_run > time.time()  # advanced, lease cleared, still live
    assert app.ctx.db.one("SELECT firing_until FROM schedules WHERE id=?", (s.id,))["firing_until"] is None


async def test_agent_tools_create_list_cancel(app, chan):
    await chan.say('[[tool:remind_me:{"when": "in 1h", "text": "stand up"}]]')
    items = app.ctx.schedules.list("alice")
    assert len(items) == 1 and items[0].mode == "message" and items[0].payload == "stand up"
    assert items[0].address == Address("fake", "c1")
    assert "stand up" in await chan.say("/schedules")
    assert "Cancelled" in await chan.say(f"/unschedule {items[0].id}")
    assert app.ctx.schedules.list("alice") == []


async def test_agent_tool_rejects_bad_when_and_limits(tmp_path):
    a = App(make_config(tmp_path, scheduler={"max_jobs_per_user": 1, "tick_s": 1}))
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    await ch.say('[[tool:remind_me:{"when": "whenever", "text": "x"}]]')
    assert a.ctx.schedules.list("alice") == []
    await ch.say('[[tool:schedule_job:{"when": "every 1h", "title": "j", "instructions": "do"}]]')
    await ch.say('[[tool:remind_me:{"when": "in 1h", "text": "second"}]]')
    assert len(a.ctx.schedules.list("alice")) == 1  # limit enforced
    a.ctx.db.close()


# ---- heartbeat ---------------------------------------------------------------------------------
def _hb_app(tmp_path):
    return App(make_config(tmp_path, heartbeat={"enabled": True, "every": "1h", "target": "fake:c1"}))


async def test_heartbeat_registered_and_checklist_created(tmp_path):
    a = _hb_app(tmp_path)
    a.scheduler._sync_heartbeat()
    hb = a.ctx.schedules.get(HEARTBEAT_ID)
    assert hb.mode == "heartbeat" and hb.expr == "3600" and hb.address == Address("fake", "c1")
    assert (a.ctx.workspace.root / "HEARTBEAT.md").exists()
    assert hb not in a.ctx.schedules.list(include_system=False)  # hidden from user listings
    first_next = hb.next_run
    a.scheduler._sync_heartbeat()  # restart keeps its schedule instead of resetting the clock
    assert a.ctx.schedules.get(HEARTBEAT_ID).next_run == first_next
    a.ctx.db.close()


async def test_heartbeat_disabled_removes_schedule(tmp_path):
    a = _hb_app(tmp_path)
    a.scheduler._sync_heartbeat()
    a2 = App(make_config(tmp_path, heartbeat={"enabled": False}))
    a2.scheduler._sync_heartbeat()
    assert a2.ctx.schedules.get(HEARTBEAT_ID) is None
    a.ctx.db.close()


async def test_heartbeat_quiet_vs_alert(tmp_path):
    a = _hb_app(tmp_path)
    await a.gateway.start()
    a.runner.start()
    ch = a.gateway.channels["fake"]
    a.scheduler._sync_heartbeat()
    hb = a.ctx.schedules.get(HEARTBEAT_ID)

    a.ctx.models.profile("a").reply = "HEARTBEAT_OK"
    await a.scheduler.fire(hb)
    await wait_for(lambda: a.ctx.tasks.list()[0].status == "succeeded")
    assert ch.out == []  # nothing to report -> silent

    a.ctx.models.profile("a").reply = "Dentist appointment at 5pm"
    await a.scheduler.fire(hb)
    await wait_for(lambda: any("Dentist" in m for m in ch.out))
    assert "HEARTBEAT_OK" in a.ctx.models.profile("a").prompt_text()  # the instruction reached the model
    await a.runner.stop()
    a.ctx.db.close()
