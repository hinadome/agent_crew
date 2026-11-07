import asyncio

from kiro_crew.app import App
from kiro_crew.core.types import Address

from .conftest import make_config, wait_for


async def _done(app, tid, statuses=("succeeded", "failed", "cancelled")):
    await wait_for(lambda: app.ctx.tasks.get(tid).status in statuses)
    return app.ctx.tasks.get(tid)


async def test_agent_starts_task_and_origin_is_notified(app, chan):
    await chan.say('[[tool:start_task:{"title": "Big job", "goal": "do it"}]]')
    t = app.ctx.tasks.list()[0]
    t = await _done(app, t.id)
    assert t.status == "succeeded" and t.title == "Big job" and t.user_id == "alice"
    await wait_for(lambda: any("done: Big job" in m for m in chan.out))


async def test_crash_recovery_requeues_and_completes(app):
    t = app.ctx.tasks.create("Recover", "g", agent=None, address=Address("fake", "c1"), user_id="alice")
    app.ctx.tasks.db.execute("UPDATE tasks SET status='running', attempts=1 WHERE id=?", (t.id,))
    assert app.ctx.tasks.recover_interrupted() == 1
    app.runner.wake()
    assert (await _done(app, t.id)).status == "succeeded"


async def test_resumed_attempt_uses_continue_prompt(app, models):
    t = app.ctx.tasks.create("Resume", "the goal", agent=None, address=None, user_id="u")
    app.ctx.tasks.db.execute("UPDATE tasks SET status='running', attempts=1 WHERE id=?", (t.id,))
    app.ctx.tasks.recover_interrupted()
    app.runner.wake()
    await _done(app, t.id)
    assert "You were interrupted" in models["A"].prompt_text()


async def test_final_failure_marks_failed_and_notifies(tmp_path):
    a = App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 1}))
    await a.gateway.start()
    a.runner.start()
    a.ctx.models.profile("a").fail = True
    t = a.ctx.tasks.create("Bad", "g", agent=None, address=Address("fake", "c1"), user_id="u")
    done = await _done(a, t.id)
    assert done.status == "failed" and "model exploded" in done.error
    ch = a.gateway.channels["fake"]
    await wait_for(lambda: any("failed after 1" in m for m in ch.out))
    await a.runner.stop()
    a.ctx.db.close()


async def test_failure_with_attempts_left_requeues_with_backoff(tmp_path):
    a = App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 3}))
    await a.gateway.start()
    a.runner.start()
    a.ctx.models.profile("a").fail = True
    t = a.ctx.tasks.create("Retry", "g", agent=None, address=None, user_id="u")
    await wait_for(lambda: a.ctx.tasks.get(t.id).error)
    row = a.ctx.tasks.get(t.id)
    assert row.status == "queued" and row.attempts == 1
    nb = a.ctx.db.one("SELECT not_before FROM tasks WHERE id=?", (t.id,))["not_before"]
    import time

    assert 20 < nb - time.time() <= 31  # first backoff is 30 s
    await a.runner.stop()
    a.ctx.db.close()


async def test_timeout_is_reported_clearly(tmp_path):
    a = App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 1, "task_timeout_s": 1}))
    await a.gateway.start()
    a.runner.start()
    a.ctx.models.profile("a").delay = 30
    t = a.ctx.tasks.create("Slow", "g", agent=None, address=Address("fake", "c1"), user_id="u")
    done = await _done(a, t.id)
    assert done.status == "failed" and done.error == "timed out after 1s"
    await a.runner.stop()
    a.ctx.db.close()


async def test_cancel_running_task(app, models):
    app.ctx.models.profile("a").delay = 30
    t = app.ctx.tasks.create("Long", "g", agent=None, address=Address("fake", "c1"), user_id="u")
    await wait_for(lambda: app.ctx.tasks.get(t.id).status == "running")
    assert app.runner.cancel(t.id)
    assert (await _done(app, t.id)).status == "cancelled"
    assert not app.runner.cancel("nope")


async def test_report_progress_and_task_agent_tool_set(app):
    ag = app.factory.create(None, session_id="task-x", user_id="u", task_id="t1")
    assert "report_progress" in ag.tool_names
    assert not {"start_task", "remind_me", "schedule_job"} & set(ag.tool_names)  # no recursion
    t = app.ctx.tasks.create("P", "g", agent=None, address=None, user_id="u")
    ag2 = app.factory.create(None, session_id="task-y", user_id="u", task_id=t.id)
    await ag2.invoke_async('[[tool:report_progress:{"note": "half done"}]]')
    assert "half done" in " ".join(app.ctx.tasks.get(t.id).progress)


async def test_quiet_token_suppresses_notification(app, chan, models):
    app.ctx.models.profile("a").reply = "HEARTBEAT_OK nothing to do"
    t = app.ctx.tasks.create("Quiet", "g", agent=None, address=Address("fake", "c1"), user_id="u",
                             quiet_token="HEARTBEAT_OK")
    done = await _done(app, t.id)
    await asyncio.sleep(0.2)
    assert done.status == "succeeded" and chan.out == []


async def test_tasks_and_cancel_commands(app, chan):
    t = app.ctx.tasks.create("Mine", "g", agent=None, address=None, user_id="alice")
    app.ctx.tasks.cancel(t.id)
    assert "Mine" in await chan.say("/tasks")
    assert "Usage" in await chan.say("/cancel")
