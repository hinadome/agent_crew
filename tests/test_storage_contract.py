"""Behavioural contract every storage backend must satisfy. Each test runs against ``sqlite`` AND ``memory``.

If you write a new backend, add its name to ``BACKENDS`` (or parametrise with your own builder) and make
these pass.
"""

import time

import pytest
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from kiro_crew.core.types import Address
from kiro_crew.memory.longterm import memory_backends
from kiro_crew.memory.workspace import Workspace
from kiro_crew.storage import build_storage

from .conftest import make_config

BACKENDS = ["sqlite", "memory"]
ADDR = Address("fake", "c1")


@pytest.fixture(params=BACKENDS)
def st(request, tmp_path):
    cfg = make_config(tmp_path, storage={"backend": request.param})
    storage = build_storage(cfg, Workspace(cfg.workspace))
    yield storage
    storage.close()


def _task(st, title="t", **kw):
    return st.tasks.create(title, "goal", agent=kw.pop("agent", None), address=kw.pop("address", ADDR),
                           user_id=kw.pop("user_id", "u"), **kw)


# ---- tasks ---------------------------------------------------------------------------------------
def test_task_roundtrip_and_defaults(st):
    t = _task(st, quiet_token="OK")
    got = st.tasks.get(t.id)
    assert (got.title, got.goal, got.status, got.attempts, got.user_id, got.quiet_token) == ("t", "goal", "queued", 0, "u", "OK")
    assert got.address == ADDR and got.session_id == f"task-{t.id}" and got.progress == []
    assert st.tasks.get("nope") is None


def test_task_list_filters_and_order(st):
    a, b, c = _task(st, "a", user_id="x"), _task(st, "b", user_id="y"), _task(st, "c", user_id="x")
    time.sleep(0.01)
    assert {t.id for t in st.tasks.list()} == {a.id, b.id, c.id}
    assert {t.id for t in st.tasks.list(user_id="x")} == {a.id, c.id}
    assert len(st.tasks.list(limit=2)) == 2
    st.tasks.cancel(a.id)
    assert {t.id for t in st.tasks.list(active_only=True)} == {b.id, c.id}


def test_claim_is_fifo_exclusive_and_counts_attempts(st):
    first = _task(st, "first")
    time.sleep(0.01)
    second = _task(st, "second")
    c1, c2 = st.tasks.claim_next(), st.tasks.claim_next()
    assert (c1.id, c1.status, c1.attempts) == (first.id, "running", 1)
    assert c2.id == second.id and c1.id != c2.id
    assert st.tasks.claim_next() is None


def test_requeue_applies_backoff_and_only_to_running(st):
    t = _task(st)
    st.tasks.requeue(t.id, delay_s=0)  # not running -> no effect
    assert st.tasks.get(t.id).status == "queued" and st.tasks.get(t.id).error is None
    st.tasks.claim_next()
    st.tasks.requeue(t.id, delay_s=60, error="boom")
    got = st.tasks.get(t.id)
    assert (got.status, got.error) == ("queued", "boom")
    assert st.tasks.claim_next() is None  # not_before is in the future


def test_finish_never_overwrites_terminal_state(st):
    t = _task(st)
    st.tasks.claim_next()
    assert st.tasks.cancel(t.id) and not st.tasks.cancel(t.id)
    st.tasks.finish(t.id, "succeeded", result="late")
    assert st.tasks.get(t.id).status == "cancelled"
    t2 = _task(st)
    st.tasks.claim_next()
    st.tasks.finish(t2.id, "succeeded", result="done")
    assert (st.tasks.get(t2.id).status, st.tasks.get(t2.id).result) == ("succeeded", "done")


def test_recover_interrupted_requeues_running_only(st):
    running, queued = _task(st, "r"), _task(st, "q")
    st.tasks.claim_next()  # oldest -> running
    assert st.tasks.recover_interrupted() == 1
    assert {st.tasks.get(running.id).status, st.tasks.get(queued.id).status} == {"queued"}


def test_progress_is_capped_and_timestamped(st):
    t = _task(st)
    for i in range(60):
        st.tasks.add_progress(t.id, f"step {i}")
    notes = st.tasks.get(t.id).progress
    assert len(notes) == 50 and notes[-1].endswith("step 59") and notes[0].endswith("step 10")
    st.tasks.add_progress("missing", "ignored")  # unknown id is a no-op


def test_purge_finished_and_active_session_ids(st, monkeypatch):
    done, live, running = _task(st, "d"), _task(st, "l"), _task(st, "r")
    st.tasks.finish(done.id, "succeeded", result="x")
    st.tasks.claim_next()  # oldest queued is `live`... claim order makes it running
    assert st.tasks.active_session_ids() == {f"task-{live.id}", f"task-{running.id}"}
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 40 * 86400)
    assert st.tasks.purge_finished(30 * 86400) == 1
    assert st.tasks.get(done.id) is None and st.tasks.get(live.id) is not None


# ---- schedules -----------------------------------------------------------------------------------
def _sched(st, kind="every", expr="60", first=None, mode="message", user="u", **kw):
    return st.schedules.create(kind=kind, expr=expr, first_run=time.time() - 1 if first is None else first,
                               mode=mode, title="t", payload="p", agent=None, address=ADDR, user_id=user, **kw)


def test_schedule_roundtrip_validation_and_replace(st):
    s = _sched(st, id="fixed")
    got = st.schedules.get("fixed")
    assert (got.mode, got.kind, got.address, got.user_id, got.run_count) == ("message", "every", ADDR, "u", 0)
    with pytest.raises(ValueError, match="mode"):
        _sched(st, mode="bogus")
    st.schedules.create(kind="every", expr="120", first_run=time.time() + 99, mode="task", title="new", payload="q",
                        agent=None, address=None, user_id="u", id="fixed")
    assert st.schedules.get("fixed").expr == "120" and st.schedules.get(s.id).mode == "task"  # upsert by id


def test_schedule_lease_complete_release(st):
    one = _sched(st, "at", str(time.time() - 1))
    rec = _sched(st)
    later = _sched(st, first=time.time() + 3600)
    claimed = {s.id for s in st.schedules.claim_due("UTC")}
    assert claimed == {one.id, rec.id}
    assert st.schedules.claim_due("UTC") == []  # leased: not re-claimed
    assert st.schedules.get(rec.id).run_count == 0  # leasing does not advance
    for sid in claimed:
        st.schedules.complete_firing(sid, "UTC")
    assert st.schedules.get(one.id).enabled is False  # one-shot finished
    got = st.schedules.get(rec.id)
    assert got.run_count == 1 and got.next_run > time.time() and got.enabled
    assert st.schedules.get(later.id).run_count == 0


def test_schedule_expired_lease_is_reclaimed_and_release_retries(st):
    s = _sched(st, "at", str(time.time() - 1))
    assert [x.id for x in st.schedules.claim_due("UTC", lease_s=300)] == [s.id]
    assert st.schedules.claim_due("UTC") == []
    assert [x.id for x in st.schedules.claim_due("UTC", now=time.time() + 400)] == [s.id]  # crash recovery
    st.schedules.release_firing(s.id)
    assert [x.id for x in st.schedules.claim_due("UTC")] == [s.id]  # released -> immediate retry


def test_missed_recurring_fires_once_then_resumes(st):
    s = _sched(st, first=time.time() - 3600)
    assert len(st.schedules.claim_due("UTC")) == 1
    st.schedules.complete_firing(s.id, "UTC")
    assert st.schedules.claim_due("UTC") == [] and st.schedules.get(s.id).next_run > time.time()


def test_schedule_list_count_delete_scoping(st):
    mine, theirs = _sched(st, user="me"), _sched(st, user="other")
    hb = _sched(st, user="shared", id="heartbeat")
    assert [s.id for s in st.schedules.list("me")] == [mine.id]
    assert hb.id not in {s.id for s in st.schedules.list()}  # system schedule hidden...
    assert hb.id in {s.id for s in st.schedules.list(include_system=True)}  # ...unless asked for
    assert st.schedules.count("me") == 1 and st.schedules.count("nobody") == 0
    assert not st.schedules.delete(theirs.id, user_id="me") and st.schedules.delete(theirs.id)


def test_cron_schedule_advances_in_timezone(st):
    s = _sched(st, "cron", "0 9 * * *", first=time.time() - 1)
    st.schedules.claim_due("Asia/Tokyo")
    st.schedules.complete_firing(s.id, "Asia/Tokyo")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    nxt = datetime.fromtimestamp(st.schedules.get(s.id).next_run, ZoneInfo("Asia/Tokyo"))
    assert (nxt.hour, nxt.minute) == (9, 0)


# ---- pairing -------------------------------------------------------------------------------------
def test_pairing_code_lifecycle(st):
    p = st.pairing
    code, notify = p.request("discord", "42", "Zed")
    assert notify and len(code) == 8
    assert p.request("discord", "42") == (code, False)  # same live code, throttled notification
    assert [(r.channel, r.sender_id, r.display_name) for r in p.pending()] == [("discord", "42", "Zed")]
    assert p.approve("WRONGCODE") is None and not p.is_approved("discord", "42")
    req = p.approve(code.lower())  # case-insensitive
    assert (req.channel, req.sender_id) == ("discord", "42") and p.is_approved("discord", "42")
    assert p.approve(code) is None and p.pending() == []  # single use
    assert p.approved() == [("discord", "42")]
    assert p.revoke("discord", "42") and not p.revoke("discord", "42") and not p.is_approved("discord", "42")


def test_pairing_expiry_cap_and_renotify(st, monkeypatch):
    p = st.pairing
    code, _ = p.request("slack", "a")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 700)
    assert p.request("slack", "a") == (code, True)  # throttle window (10 min) elapsed -> notify again
    monkeypatch.setattr(time, "time", lambda: real() + 7200)
    assert p.approve(code) is None  # expired after an hour
    monkeypatch.setattr(time, "time", real)
    assert [r.sender_id for r in p.pending()] == ["a"]  # "a" is still pending and holds one of the 20 slots
    for i in range(19):
        assert p.request("slack", f"s{i}") is not None
    assert p.request("slack", "overflow") is None  # pending queue is capped at 20


# ---- settings ------------------------------------------------------------------------------------
def test_settings_model_profile(st):
    assert st.settings.get_model_profile("k") is None
    st.settings.set_model_profile("k", "gpt")
    st.settings.set_model_profile("k", "claude")  # overwrite
    assert st.settings.get_model_profile("k") == "claude" and st.settings.get_model_profile("other") is None


def test_settings_agent_override_is_independent_of_the_model_profile(st):
    assert st.settings.get_agent("k") is None
    st.settings.set_model_profile("k", "gpt")
    st.settings.set_agent("k", "support")
    assert (st.settings.get_agent("k"), st.settings.get_model_profile("k")) == ("support", "gpt")  # neither clobbers
    st.settings.set_agent("k", "research")
    assert st.settings.get_agent("k") == "research" and st.settings.get_model_profile("k") == "gpt"
    st.settings.set_agent("k", None)  # reset to default routing
    assert st.settings.get_agent("k") is None and st.settings.get_model_profile("k") == "gpt"
    st.settings.set_agent("only-agent", "x")  # a row that never had a model profile
    assert st.settings.get_model_profile("only-agent") is None


# ---- sessions ------------------------------------------------------------------------------------
def _msg(i, text="hi"):
    return SessionMessage(message={"role": "user", "content": [{"text": f"{text} {i}"}]}, message_id=i)


def test_session_agent_message_roundtrip(st):
    s = st.sessions
    s.create_session(Session("s1", SessionType.AGENT))
    with pytest.raises(SessionException):
        s.create_session(Session("s1", SessionType.AGENT))
    assert s.read_session("s1").session_id == "s1" and s.read_session("nope") is None
    s.create_agent("s1", SessionAgent("a", {"k": 1}, {}))
    original = s.read_agent("s1", "a").created_at
    s.update_agent("s1", SessionAgent("a", {"k": 2}, {}))
    got = s.read_agent("s1", "a")
    assert got.state == {"k": 2} and got.created_at == original  # created_at preserved
    with pytest.raises(SessionException):
        s.update_agent("s1", SessionAgent("ghost", {}, {}))
    for i in (2, 0, 1):  # insert out of order
        s.create_message("s1", "a", _msg(i))
    assert [m.message_id for m in s.list_messages("s1", "a")] == [0, 1, 2]
    assert [m.message_id for m in s.list_messages("s1", "a", limit=2, offset=1)] == [1, 2]
    first_created = s.read_message("s1", "a", 0).created_at
    s.update_message("s1", "a", _msg(0, "edited"))
    got = s.read_message("s1", "a", 0)
    assert "edited" in got.message["content"][0]["text"] and got.created_at == first_created
    with pytest.raises(SessionException):
        s.update_message("s1", "a", _msg(99))
    assert s.read_message("s1", "a", 99) is None


def test_session_reads_are_isolated_copies(st):
    """Mutating a returned object must not change stored state (true for persistence AND the memory backend)."""
    s = st.sessions
    s.create_session(Session("s", SessionType.AGENT))
    s.create_agent("s", SessionAgent("a", {"k": 1}, {}))
    s.read_agent("s", "a").state["k"] = 999
    assert s.read_agent("s", "a").state == {"k": 1}


def test_delete_session_removes_everything(st):
    s = st.sessions
    s.create_session(Session("gone", SessionType.AGENT))
    s.create_agent("gone", SessionAgent("a", {}, {}))
    s.create_message("gone", "a", _msg(0))
    s.delete_session("gone")
    assert s.read_session("gone") is None and s.read_agent("gone", "a") is None
    assert s.read_message("gone", "a", 0) is None
    s.delete_session("never-existed")  # idempotent


def test_prune_idle_respects_age_and_protected(st, monkeypatch):
    s = st.sessions
    for sid in ("old", "task-live"):
        s.create_session(Session(sid, SessionType.AGENT))
        s.create_agent(sid, SessionAgent("a", {}, {}))
        s.create_message(sid, "a", _msg(0))
    assert s.prune_idle(90 * 86400) == 0  # everything is fresh
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 200 * 86400)
    s.create_session(Session("fresh", SessionType.AGENT))  # created "now" (in the shifted clock)
    s.create_agent("fresh", SessionAgent("a", {}, {}))
    s.create_message("fresh", "a", _msg(0))
    assert s.prune_idle(90 * 86400, protected={"task-live"}) == 1  # only "old"
    assert {sid for sid in ("old", "task-live", "fresh") if s.read_session(sid)} == {"task-live", "fresh"}


# ---- long-term fact stores (sqlite keyword store and the in-process one) --------------------------
@pytest.fixture(params=["sqlite", "inmem"])
def facts(request, tmp_path):
    backend = "sqlite" if request.param == "sqlite" else "memory"
    cfg = make_config(tmp_path, storage={"backend": backend}, memory={"backend": request.param, "auto_extract": False})
    storage = build_storage(cfg, Workspace(cfg.workspace))
    import kiro_crew.memory.factory  # noqa: F401  (registers backends)

    yield lambda ns: memory_backends.get(request.param)(storage, ns)
    storage.close()


async def test_fact_store_contract(facts):
    a, b = facts("user:a"), facts("user:b")
    assert await a.add("Alice prefers dark mode; deploys on Fridays")
    assert not await a.add("Alice prefers dark mode; deploys on Fridays")  # idempotent
    assert not await a.add("   ")
    hits = await a.search("which mode does she prefer?")
    assert hits and hits[0].content.startswith("Alice")
    assert await a.search("*") == [] and await a.search('"; DROP TABLE x --') == []
    assert await b.search("dark mode") == [] and b.count() == 0  # namespace isolation
    assert not b.delete(a.list()[0]["id"]) and b.clear() == 0 and a.count() == 1
    await a.add("second fact about tea")
    assert next(f["content"] for f in a.list()) == "second fact about tea"  # newest first
    fid = a.list()[0]["id"]
    assert a.delete(fid) and await a.search("tea") == [] and a.count() == 1
    assert a.clear() == 1 and a.count() == 0


def test_delete_prefix_is_literal_not_a_wildcard_pattern(st):
    """Session keys contain '_' and '%' (LIKE wildcards); the prefix match must treat them literally."""
    s = st.sessions
    for sid in ("conv_1", "conv_1~helper", "conv_1~other", "convX1~helper", "conv_10", "other"):
        s.create_session(Session(sid, SessionType.AGENT))
    assert s.delete_prefix("conv_1~") == 2  # exactly the delegate sessions of conv_1
    left = {sid for sid in ("conv_1", "conv_1~helper", "conv_1~other", "convX1~helper", "conv_10", "other")
            if s.read_session(sid)}
    assert left == {"conv_1", "convX1~helper", "conv_10", "other"}  # 'X' did not match '_'; the parent is untouched
    assert s.delete_prefix("nothing-matches") == 0
    s.create_session(Session("50%_off", SessionType.AGENT))
    s.create_session(Session("500_off", SessionType.AGENT))
    assert s.delete_prefix("50%") == 1 and s.read_session("500_off") is not None


def test_task_done_when_roundtrip(st):
    t = st.tasks.create("t", "goal", agent=None, address=ADDR, user_id="u", done_when="file x exists")
    assert st.tasks.get(t.id).done_when == "file x exists"
    assert st.tasks.create("t2", "goal", agent=None, address=ADDR, user_id="u").done_when is None


# ---- asking the user (WAITING) -------------------------------------------------------------------------
def test_ask_pauses_a_running_task_and_answer_resumes_it(st):
    t = _task(st)
    assert not st.tasks.ask(t.id, "q?", "dflt", 9e9)  # only a RUNNING task can ask
    st.tasks.claim_next()
    assert st.tasks.ask(t.id, "Which dates?", "next weekend", 9e9)
    w = st.tasks.get(t.id)
    assert (w.status, w.question, w.question_default, w.asks) == ("waiting", "Which dates?", "next weekend", 1)
    assert st.tasks.claim_next() is None  # a waiting task is not picked up by a worker
    assert [x.id for x in st.tasks.list(active_only=True)] == [t.id]
    assert t.session_id in st.tasks.active_session_ids()  # housekeeping must not prune its session
    assert st.tasks.recover_interrupted() == 0  # and a restart leaves it waiting
    assert st.tasks.answer(t.id, "12-15 Dec")
    assert not st.tasks.answer(t.id, "again")  # no longer waiting
    r = st.tasks.get(t.id)
    assert (r.status, r.answer, r.attempts) == ("queued", "12-15 Dec", 0)  # answering is not a retry
    assert st.tasks.claim_next().attempts == 1
    st.tasks.consume_answer(t.id)
    c = st.tasks.get(t.id)
    assert (c.question, c.answer, c.question_default, c.ask_deadline) == (None, None, None, None)
    assert c.asks == 1


def test_unanswered_questions_expire_into_their_default(st):
    a, b = _task(st, "a"), _task(st, "b")
    st.tasks.claim_next()
    st.tasks.claim_next()
    st.tasks.ask(a.id, "q", "use X", 1.0)  # long overdue
    st.tasks.ask(b.id, "q", "use Y", 9e9)  # not yet
    assert st.tasks.expire_questions() == 1
    got = st.tasks.get(a.id)
    assert got.status == "queued" and "did not answer" in got.answer and "use X" in got.answer
    assert st.tasks.get(b.id).status == "waiting"


def test_waiting_tasks_can_be_cancelled_and_summarise_their_question(st):
    t = _task(st)
    st.tasks.claim_next()
    st.tasks.ask(t.id, "Which dates?", "d", 9e9)
    assert f"/answer {t.id}" in st.tasks.get(t.id).summary() and "Which dates?" in st.tasks.get(t.id).summary()
    assert st.tasks.cancel(t.id) and st.tasks.get(t.id).status == "cancelled"


def test_restart_recovery_forgets_the_last_error(st):
    t = _task(st)
    st.tasks.claim_next()
    st.tasks.requeue(t.id, error="boom")
    st.tasks.claim_next()
    assert st.tasks.recover_interrupted() == 1
    assert st.tasks.get(t.id).error is None  # an interrupted run is "continue", not "you failed"
