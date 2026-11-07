"""Sub-tasks fan out in parallel, the parent waits without holding a worker, then continues with their results."""

from types import SimpleNamespace

from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.tasks.subtasks import results_text, subtask_tools

from .conftest import make_config, wait_for
from .test_autonomy import _said


def _app(tmp_path, workers=1, **tasks):
    return App(make_config(tmp_path, tasks={"workers": workers, "max_attempts": 1, **tasks}))


async def _boot(a):
    await a.gateway.start()
    a.runner.start()
    return a.gateway.channels["fake"]


async def _close(a):
    await a.runner.stop()
    await a.gateway.stop()
    a.ctx.db.close()


def _tools(a, tid):
    return {t.tool_name: t for t in subtask_tools(a.ctx, tid)}


_CTX = SimpleNamespace(agent=SimpleNamespace(state=SimpleNamespace(set=lambda k, v: None)))


async def _parent_waiting_on_two(a):
    """A RUNNING parent that started two sub-tasks and called wait_for_subtasks (as the agent would)."""
    p = a.ctx.tasks.create("Compare cities", "compare Lisbon and Porto", agent=None,
                           address=Address("fake", "c1"), user_id="alice")
    a.ctx.tasks.db.execute("UPDATE tasks SET status='running', attempts=1 WHERE id=?", (p.id,))
    t = _tools(a, p.id)
    t["start_subtask"](title="Lisbon", goal="research Lisbon", tool_context=_CTX)
    t["start_subtask"](title="Porto", goal="research Porto", done_when="mentions the river", tool_context=_CTX)
    assert "Paused" in t["wait_for_subtasks"](tool_context=_CTX)
    return p


async def test_parent_waits_children_run_on_the_only_worker_then_parent_gets_results(tmp_path):
    a = _app(tmp_path, workers=1, verify=False)  # one worker: a parent that held it would deadlock
    ch = await _boot(a)
    p = await _parent_waiting_on_two(a)
    a.runner.wake()
    await wait_for(lambda: a.ctx.tasks.get(p.id).status == "succeeded", timeout=15)
    kids = a.ctx.tasks.children(p.id)
    assert [k.status for k in kids] == ["succeeded", "succeeded"] and all(k.address is None for k in kids)
    assert kids[1].done_when == "mentions the river" and all(k.user_id == "alice" for k in kids)
    seen = a.ctx.models.profile("a").prompt_text()
    assert "Your sub-tasks have finished" in seen and "Lisbon: SUCCEEDED" in seen and "Porto: SUCCEEDED" in seen
    parent = a.ctx.tasks.get(p.id)
    assert parent.attempts == 1 and any("all sub-tasks finished" in x for x in parent.progress)
    await wait_for(lambda: f"Task [{p.id}] done" in _said(ch))
    assert not any(f"Task [{k.id}]" in _said(ch) for k in kids)  # children report to the parent, not the chat
    await _close(a)


async def test_children_run_in_parallel_with_two_workers(tmp_path):
    a = _app(tmp_path, workers=2, verify=False)
    await _boot(a)
    model = a.ctx.models.profile("a")
    model.delay = 0.5
    p = await _parent_waiting_on_two(a)
    a.runner.wake()
    import time

    t0 = time.monotonic()
    await wait_for(lambda: all(k.status == "succeeded" for k in a.ctx.tasks.children(p.id)), timeout=15)
    assert time.monotonic() - t0 < 1.0 + 0.6  # ~0.5 s in parallel, not ~1.0 s+ in sequence
    await wait_for(lambda: a.ctx.tasks.get(p.id).status == "succeeded", timeout=15)
    await _close(a)


async def test_failed_or_cancelled_children_are_reported_to_the_parent(tmp_path):
    a = _app(tmp_path)
    p = await _parent_waiting_on_two(a)
    k1, k2 = a.ctx.tasks.children(p.id)
    a.ctx.tasks.cancel(k1.id)
    a.ctx.tasks.db.execute("UPDATE tasks SET status='failed', error='HTTP 503' WHERE id=?", (k2.id,))
    assert a.runner._resume_parents() == 1
    got = a.ctx.tasks.get(p.id)
    assert got.status == "queued" and "Lisbon: CANCELLED" in got.answer and "HTTP 503" in got.answer
    prompt = a.runner._prompt(got)
    assert "redo or cover anything that failed" in prompt
    a.ctx.db.close()


async def test_cancelling_the_parent_cancels_its_children(tmp_path):
    a = _app(tmp_path)
    p = await _parent_waiting_on_two(a)
    assert a.runner.cancel(p.id)
    assert {k.status for k in a.ctx.tasks.children(p.id)} == {"cancelled"}
    a.ctx.db.close()


async def test_limits_one_level_and_already_finished(tmp_path):
    a = _app(tmp_path, max_subtasks=2)
    p = await _parent_waiting_on_two(a)
    assert "Limit reached" in _tools(a, p.id)["start_subtask"](title="x", goal="g", tool_context=_CTX)
    factory = a.ctx.services["agent_factory"]
    child = a.ctx.tasks.children(p.id)[0]
    names = set(factory.create(None, session_id=None, user_id="u", task_id=child.id).tool_names)
    assert "start_subtask" not in names and "update_plan" in names  # sub-tasks cannot fan out again
    top = a.ctx.tasks.create("top", "g", agent=None, address=None, user_id="u")
    assert "start_subtask" in factory.create(None, session_id=None, user_id="u", task_id=top.id).tool_names
    lone = a.ctx.tasks.create("lone", "g", agent=None, address=None, user_id="u")
    assert "no sub-tasks" in _tools(a, lone.id)["wait_for_subtasks"](tool_context=_CTX)
    for k in a.ctx.tasks.children(p.id):
        a.ctx.tasks.db.execute("UPDATE tasks SET status='succeeded', result='R' WHERE id=?", (k.id,))
    a.ctx.tasks.db.execute("UPDATE tasks SET status='running', wait_kind=NULL WHERE id=?", (p.id,))
    assert "already finished" in _tools(a, p.id)["wait_for_subtasks"](tool_context=_CTX)
    a.ctx.db.close()


async def test_feature_off(tmp_path):
    a = _app(tmp_path, max_subtasks=0)
    top = a.ctx.tasks.create("top", "g", agent=None, address=None, user_id="u")
    names = a.ctx.services["agent_factory"].create(None, session_id=None, user_id="u", task_id=top.id).tool_names
    assert "start_subtask" not in names
    a.ctx.db.close()


async def test_restart_while_waiting_catches_up(tmp_path):
    a = _app(tmp_path)
    p = await _parent_waiting_on_two(a)
    for k in a.ctx.tasks.children(p.id):  # children finished while the app was down
        a.ctx.tasks.db.execute("UPDATE tasks SET status='succeeded', result='done' WHERE id=?", (k.id,))
    a.ctx.db.close()
    b = _app(tmp_path)
    await _boot(b)
    await wait_for(lambda: b.ctx.tasks.get(p.id).status == "succeeded", timeout=15)
    await _close(b)


def test_results_text_truncates():
    from kiro_crew.tasks.store import Task

    big = Task("i", "t", "g", "succeeded", None, None, None, 1, result="x" * 5000)
    assert "[truncated]" in results_text([big]) and len(results_text([big])) < 3200
