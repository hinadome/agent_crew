"""A background task can pause, ask the user a question, and resume with the answer; retries explain failures."""

import asyncio

from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.tasks.tools import ask_user_tool

from .conftest import make_config, wait_for
from .test_autonomy import _said

ASK = '[[tool:ask_user:{"question": "Which dates?", "default_assumption": "next weekend"}]]'


def _make(tmp_path, **tasks):
    return App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 2, **tasks}))


async def _boot(a):
    await a.gateway.start()
    a.runner.start()
    return a.gateway.channels["fake"]


async def _close(a):
    await a.runner.stop()
    await a.gateway.stop()
    a.ctx.db.close()


CHAT = Address("fake", "c1")


def _task(a, title, user="alice", address=CHAT, **kw):
    return a.ctx.tasks.create(title, "find flights", agent=None, address=address, user_id=user, **kw)


async def _status(a, tid, *statuses):
    await wait_for(lambda: a.ctx.tasks.get(tid).status in statuses)
    return a.ctx.tasks.get(tid)


async def test_task_asks_waits_without_blocking_others_then_resumes_with_the_answer(tmp_path):
    a = _make(tmp_path)  # a single worker: a waiting task must not hold it
    ch = await _boot(a)
    t = _task(a, ASK)
    w = await _status(a, t.id, "waiting", "succeeded", "failed")
    assert w.status == "waiting" and w.question == "Which dates?"
    await wait_for(lambda: "needs your input: Which dates?" in _said(ch))
    assert f"/answer {t.id}" in _said(ch) and "next weekend" in _said(ch)
    assert not any("done:" in m for m in ch.out)  # a paused task is not reported as finished

    other = _task(a, "unrelated job")  # runs while the first one waits
    assert (await _status(a, other.id, "succeeded", "failed")).status == "succeeded"

    assert "Thanks" in await ch.say(f"/answer {t.id} 12-15 December, under 300 EUR")
    done = await _status(a, t.id, "succeeded", "failed")
    assert done.status == "succeeded" and done.attempts == 1 and done.question is None
    seen = a.ctx.models.profile("a").prompt_text()
    assert "12-15 December, under 300 EUR" in seen and "Which dates?" in seen
    await wait_for(lambda: f"Task [{t.id}] done" in _said(ch))
    await _close(a)


async def test_unanswered_question_falls_back_to_the_stated_default(tmp_path):
    a = _make(tmp_path, ask_timeout_s=0)
    await _boot(a)
    t = _task(a, ASK)
    for _ in range(100):  # the worker loop checks for expired questions each time it wakes
        a.runner.wake()
        if a.ctx.tasks.get(t.id).status == "succeeded":
            break
        await asyncio.sleep(0.1)
    assert a.ctx.tasks.get(t.id).status == "succeeded"
    seen = a.ctx.models.profile("a").prompt_text()
    assert "did not answer in time" in seen and "next weekend" in seen
    await _close(a)


async def test_only_the_owner_can_answer_and_only_while_waiting(tmp_path):
    a = _make(tmp_path)
    ch = await _boot(a)
    t = _task(a, ASK)
    await _status(a, t.id, "waiting")
    foreign = _task(a, "x", user="bob")
    assert "No such task" in await ch.say(f"/answer {foreign.id} hi")
    assert "No such task" in await ch.say("/answer nope hi")
    assert "Usage" in await ch.say("/answer")
    assert "Usage" in await ch.say(f"/answer {t.id}")
    assert a.ctx.tasks.get(t.id).status == "waiting"  # none of that changed it
    assert "Thanks" in await ch.say(f"/answer {t.id} ok")
    await _status(a, t.id, "succeeded")
    assert "not waiting" in await ch.say(f"/answer {t.id} again")
    await _close(a)


async def test_waiting_task_can_be_cancelled(tmp_path):
    a = _make(tmp_path)
    ch = await _boot(a)
    t = _task(a, ASK)
    await _status(a, t.id, "waiting")
    assert "Cancelled" in await ch.say(f"/cancel {t.id}")
    assert a.ctx.tasks.get(t.id).status == "cancelled"
    await _close(a)


async def test_question_limit_and_nobody_to_ask(tmp_path):
    a = _make(tmp_path, max_questions=1)
    await _boot(a)
    t = _task(a, "plain")
    await _status(a, t.id, "succeeded")
    ask = ask_user_tool(a.ctx, t.id)
    a.ctx.tasks.db.execute("UPDATE tasks SET asks=1 WHERE id=?", (t.id,))
    assert "no more are allowed" in await ask(question="q", default_assumption="d")
    nobody = _task(a, "no chat", address=None)
    assert "nobody to ask" in await ask_user_tool(a.ctx, nobody.id)(question="q", default_assumption="use Z")
    await _close(a)


async def test_prompt_says_whether_asking_is_allowed(tmp_path):
    a = _make(tmp_path)
    await _boot(a)
    t = _task(a, "one")
    await _status(a, t.id, "succeeded")
    assert "call `ask_user`" in a.ctx.models.profile("a").system_text()
    await _close(a)
    b = _make(tmp_path / "off", ask_user=False)
    await _boot(b)
    t2 = _task(b, "two")
    await _status(b, t2.id, "succeeded")
    assert "Do not ask the user questions" in b.ctx.models.profile("a").system_text()
    await _close(b)


# ---- retries explain what went wrong ------------------------------------------------------------------
def _attempt(a, error=None, attempts=2, **kw):
    t = _task(a, "job", **kw)
    a.ctx.tasks.db.execute("UPDATE tasks SET attempts=?, error=? WHERE id=?", (attempts, error, t.id))
    return a.ctx.tasks.get(t.id)


async def test_retry_prompt_depends_on_why_the_last_attempt_ended(tmp_path):
    a = _make(tmp_path)
    r = a.runner
    first = r._prompt(_attempt(a, attempts=1))
    assert first.startswith("Task: job") and "FAILED" not in first

    failed = r._prompt(_attempt(a, "RuntimeError: HTTP 503 from search"))
    assert "FAILED with this error" in failed and "HTTP 503" in failed and "Do not repeat the same approach" in failed

    slow = r._prompt(_attempt(a, "timed out after 3600s"))
    assert "ran out of time" in slow and "partial" in slow and "FAILED" not in slow

    crashed = r._prompt(_attempt(a, None))  # restart/crash: nothing failed, just continue
    assert "You were interrupted" in crashed and "FAILED" not in crashed

    t = _task(a, "j2", done_when="has price")
    a.ctx.tasks.db.execute("UPDATE tasks SET attempts=2, error='boom' WHERE id=?", (t.id,))
    assert "has price" in r._prompt(a.ctx.tasks.get(t.id))  # criteria survive retries
    a.ctx.db.close()


async def test_retry_records_a_lesson_in_the_progress_notes(tmp_path):
    a = _make(tmp_path)
    t = _attempt(a, "ValueError: bad input")
    a.runner._prompt(t)
    assert any("retrying after: ValueError: bad input" in p for p in a.ctx.tasks.get(t.id).progress)
    a.ctx.db.close()


async def test_failed_attempt_is_retried_with_its_error_in_the_prompt(tmp_path):
    a = _make(tmp_path, max_attempts=2)
    await _boot(a)
    model = a.ctx.models.profile("a")
    model.fail = True
    t = _task(a, "flaky")
    await wait_for(lambda: a.ctx.tasks.get(t.id).error)
    model.fail = False
    a.ctx.tasks.db.execute("UPDATE tasks SET not_before=0 WHERE id=?", (t.id,))  # skip the backoff delay
    a.runner.wake()
    assert (await _status(a, t.id, "succeeded", "failed")).status == "succeeded"
    assert "FAILED with this error" in model.prompt_text() and "model exploded" in model.prompt_text()
    await _close(a)
