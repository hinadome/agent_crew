"""Autonomy guard rails: tool-call budget, stuck-loop detection, and task completion verification."""

from types import SimpleNamespace

import pytest

from kiro_crew.agents.budget import BudgetExceeded, BudgetHook, TaskPaused, find_budget_error, find_pause
from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.tasks.verify import parse_verdict

from .conftest import make_config, wait_for


def _said(ch):
    """Everything a channel was sent, re-joined (long messages arrive in chunks)."""
    return " ".join(" ".join(ch.out).split())


class _State(dict):
    def set(self, k, v):
        self[k] = v


def _agent(tokens=0, paused=False):
    return SimpleNamespace(state=_State({"paused_for_user": True} if paused else {}),
                           event_loop_metrics=SimpleNamespace(accumulated_usage={"totalTokens": tokens}))


def _call(name="current_time", paused=False, agent=None, **args):
    return SimpleNamespace(tool_use={"name": name, "input": args}, cancel_tool=False,
                           agent=agent or _agent(paused=paused))


# ---- BudgetHook (unit) ---------------------------------------------------------------------------
def test_identical_repeats_are_cancelled_after_the_limit():
    h = BudgetHook(max_tool_calls=0, repeat_limit=2, grace_calls=5)
    events = [_call() for _ in range(4)]
    for e in events:
        h._check(e)
    assert [bool(e.cancel_tool) for e in events] == [False, False, True, True]
    assert "identical arguments" in events[2].cancel_tool and "different approach" in events[2].cancel_tool


def test_changing_arguments_or_tools_resets_the_streak():
    h = BudgetHook(max_tool_calls=0, repeat_limit=2, grace_calls=5)
    events = [_call(q="a"), _call(q="a"), _call(q="b"), _call(q="b"), _call("other", q="b"), _call("other", q="b")]
    for e in events:
        h._check(e)
    assert not any(e.cancel_tool for e in events)


def test_total_budget_cancels_with_wrap_up_advice():
    h = BudgetHook(max_tool_calls=3, repeat_limit=0, grace_calls=5)
    events = [_call(n=i) for i in range(5)]
    for e in events:
        h._check(e)
    assert [bool(e.cancel_tool) for e in events] == [False, False, False, True, True]
    assert "budget of 3" in events[3].cancel_tool and "Reply now" in events[3].cancel_tool


def test_ignoring_the_warning_stops_the_run_hard():
    h = BudgetHook(max_tool_calls=1, repeat_limit=0, grace_calls=2)
    h._check(_call(n=0))
    h._check(_call(n=1))  # blocked 1
    h._check(_call(n=2))  # blocked 2
    with pytest.raises(BudgetExceeded):
        h._check(_call(n=3))  # blocked 3 > grace


def test_budget_resets_per_invocation_but_not_for_tasks():
    chat = BudgetHook(max_tool_calls=1, repeat_limit=0, per_invocation=True)
    task = BudgetHook(max_tool_calls=1, repeat_limit=0, per_invocation=False)
    for h in (chat, task):
        h._check(_call(n=0))
        h._begin(SimpleNamespace(agent=None))
    e1, e2 = _call(n=1), _call(n=1)
    chat._check(e1)
    task._check(e2)
    assert not e1.cancel_tool and e2.cancel_tool


def test_zero_disables_the_limits():
    h = BudgetHook(max_tool_calls=0, repeat_limit=0)
    events = [_call() for _ in range(50)]
    for e in events:
        h._check(e)
    assert not any(e.cancel_tool for e in events)


def test_find_budget_error_unwraps_chains():
    inner = BudgetExceeded("x")
    wrapper = RuntimeError("wrapped")
    wrapper.original_exception = inner  # type: ignore[attr-defined]
    assert find_budget_error(wrapper) is inner
    assert find_budget_error(RuntimeError("plain")) is None


def test_after_ask_user_every_tool_call_is_blocked_then_the_run_is_cut_off():
    h = BudgetHook(max_tool_calls=0, repeat_limit=0)
    events = [_call("web_fetch", paused=True, n=i) for i in range(3)]
    for e in events:
        h._check(e)
    assert all("paused until the user answers" in e.cancel_tool for e in events)
    with pytest.raises(TaskPaused):
        h._check(_call("web_fetch", paused=True, n=9))
    h.reset()
    h._check(_call("web_fetch", paused=True, n=10))  # reset() starts a fresh count


def test_find_pause_unwraps_chains():
    inner = TaskPaused("x")
    wrapper = RuntimeError("wrapped")
    wrapper.original_exception = inner  # type: ignore[attr-defined]
    assert find_pause(wrapper) is inner and find_pause(RuntimeError()) is None


# ---- token budget ------------------------------------------------------------------------------------
def test_token_budget_soft_then_hard():
    h = BudgetHook(max_tool_calls=0, repeat_limit=0, max_tokens=1000)
    under, over = _agent(999), _agent(1000)
    e = _call(agent=under, n=1)
    h._check(e)
    assert not e.cancel_tool
    e = _call(agent=over, n=2)
    h._check(e)
    assert "token budget of 1000 is used up (1000 tokens)" in e.cancel_tool and "Reply now" in e.cancel_tool
    h._check_model(SimpleNamespace(agent=_agent(1250)))  # the wrap-up reply is still allowed
    with pytest.raises(BudgetExceeded, match="token budget"):
        h._check_model(SimpleNamespace(agent=_agent(1251)))


def test_chat_token_budget_counts_only_this_turn_and_tasks_include_earlier_attempts():
    chat = BudgetHook(max_tool_calls=0, repeat_limit=0, max_tokens=100)
    cached = _agent(5000)  # a cached chat agent that already spent a lot in earlier turns
    chat._begin(SimpleNamespace(agent=cached))
    e = _call(agent=cached, n=1)
    chat._check(e)
    assert not e.cancel_tool and chat.used(cached) == 0
    task = BudgetHook(max_tool_calls=0, repeat_limit=0, per_invocation=False, max_tokens=100, prior_tokens=90)
    e = _call(agent=_agent(10), n=1)
    task._check(e)
    assert e.cancel_tool  # 90 earlier + 10 now = budget


def test_token_budget_off_by_default():
    h = BudgetHook(max_tool_calls=0, repeat_limit=0)
    e = _call(agent=_agent(10**9), n=1)
    h._check(e)
    h._check_model(SimpleNamespace(agent=_agent(10**9)))
    assert not e.cancel_tool


async def test_task_tokens_are_recorded_across_attempts_and_cap_the_task(tmp_path):
    a = App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 1}, limits={"task_tokens": 5}))
    await a.gateway.start()
    a.runner.start()
    ok = a.ctx.tasks.create("small", "g", agent=None, address=None, user_id="u")
    await wait_for(lambda: a.ctx.tasks.get(ok.id).status == "succeeded")
    assert a.ctx.tasks.get(ok.id).tokens == 2  # the fake model reports 2 tokens per call
    assert "k tokens" in a.ctx.tasks.get(ok.id).summary()
    a.ctx.models.profile("a").always_tool = ("current_time", "{}")  # 2 tokens per call, forever
    loop = a.ctx.tasks.create("greedy", "g", agent=None, address=Address("fake", "c1"), user_id="u")
    await wait_for(lambda: a.ctx.tasks.get(loop.id).status in ("failed", "succeeded"))
    got = a.ctx.tasks.get(loop.id)
    assert got.status == "failed" and "token budget of 5" in got.error and got.tokens >= 5
    await a.runner.stop()
    a.ctx.db.close()


# ---- runaway agents, end to end ---------------------------------------------------------------------
async def test_chat_turn_that_loops_is_stopped_and_user_is_told(app, chan):
    model = app.ctx.models.profile("a")
    model.always_tool = ("current_time", "{}")
    await chan.say("do something")
    assert "I had to stop" in _said(chan) and "identical arguments" in _said(chan)
    assert model.n_tool_calls <= 3 + 3 + 1  # repeat_limit + grace_calls + the call that aborts


async def test_task_that_loops_fails_once_without_retries(tmp_path):
    a = App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 3}))
    await a.gateway.start()
    a.runner.start()
    a.ctx.models.profile("a").always_tool = ("current_time", "{}")
    t = a.ctx.tasks.create("Loop", "g", agent=None, address=Address("fake", "c1"), user_id="u")
    await wait_for(lambda: a.ctx.tasks.get(t.id).status in ("failed", "succeeded"))
    done = a.ctx.tasks.get(t.id)
    ch = a.gateway.channels["fake"]
    assert done.status == "failed" and done.attempts == 1 and "identical arguments" in done.error
    await wait_for(lambda: "stopped:" in _said(ch))
    await a.runner.stop()
    a.ctx.db.close()


# ---- verdict parsing --------------------------------------------------------------------------------
@pytest.mark.parametrize("text,met", [
    ('{"met": true, "feedback": ""}', True),
    ('Sure! {"met": false, "feedback": "no {braces} sources"} done', False),
    ('```json\n{"met": true}\n```', True),
    ('{"met": "yes"}', None),
    ("looks good to me", None),
    ("", None),
])
def test_parse_verdict(text, met):
    assert parse_verdict(text).met is met


def test_parse_verdict_keeps_feedback_with_braces():
    assert parse_verdict('{"met": false, "feedback": "no {braces} sources"}').feedback == "no {braces} sources"


# ---- verification in the task runner --------------------------------------------------------------------
async def _verified_app(tmp_path, **tasks):
    cfg = make_config(tmp_path, tasks={"workers": 1, "max_attempts": 1, **tasks},
                      models=dict(default="a", aux="b", profiles={"a": dict(provider="fake", model_id="A"),
                                                                  "b": dict(provider="fake", model_id="B")}))
    a = App(cfg)
    await a.gateway.start()
    a.runner.start()
    return a


async def _run_task(a, done_when="cites 3 sources"):
    t = a.ctx.tasks.create("Report", "write a report", agent=None, address=Address("fake", "c1"),
                           user_id="u", done_when=done_when)
    await wait_for(lambda: a.ctx.tasks.get(t.id).status in ("failed", "succeeded"))
    return a.ctx.tasks.get(t.id), a.gateway.channels["fake"]


async def _close(a):
    await a.runner.stop()
    a.ctx.db.close()


async def test_unmet_result_is_sent_back_and_the_fix_is_delivered(tmp_path):
    a = await _verified_app(tmp_path)
    worker, reviewer = a.ctx.models.profile("a"), a.ctx.models.profile("b")
    worker.replies = ["first draft", "fixed draft with sources"]
    reviewer.replies = ['{"met": false, "feedback": "only 1 source"}', '{"met": true, "feedback": ""}']
    t, ch = await _run_task(a)
    assert t.status == "succeeded" and t.result == "fixed draft with sources"
    assert "only 1 source" in worker.prompt_text() and "NOT complete" in worker.prompt_text()
    assert "cites 3 sources" in worker.prompt_text()  # criteria were given to the worker up front
    await wait_for(lambda: "fixed draft with sources" in _said(ch))
    assert "WARNING" not in _said(ch)
    assert any("verification found a gap" in p for p in t.progress) and any("verified" in p for p in t.progress)
    await _close(a)


async def test_never_met_is_delivered_with_a_visible_warning_after_bounded_rounds(tmp_path):
    a = await _verified_app(tmp_path, max_verify_rounds=2)
    worker, reviewer = a.ctx.models.profile("a"), a.ctx.models.profile("b")
    reviewer.reply = '{"met": false, "feedback": "still no sources"}'
    t, ch = await _run_task(a)
    assert t.status == "succeeded"
    assert len(worker.seen) == 3  # initial run + 2 fix rounds, then it stops
    assert len(reviewer.seen) == 3
    await wait_for(lambda: "WARNING - not confirmed complete" in _said(ch) and "still no sources" in _said(ch))
    await _close(a)


async def test_inconclusive_reviewer_never_blocks_delivery(tmp_path):
    a = await _verified_app(tmp_path)
    a.ctx.models.profile("b").reply = "no idea, sorry"
    t, ch = await _run_task(a)
    assert t.status == "succeeded"
    await wait_for(lambda: "could not be verified" in _said(ch))
    await _close(a)


async def test_reviewer_failure_never_loses_the_result(tmp_path):
    a = await _verified_app(tmp_path)
    a.ctx.models.profile("b").fail = True
    t, ch = await _run_task(a)
    assert t.status == "succeeded" and t.result
    await wait_for(lambda: "done: Report" in _said(ch))
    await _close(a)


async def test_no_criteria_means_no_verification_call(tmp_path):
    a = await _verified_app(tmp_path)
    t, _ = await _run_task(a, done_when=None)
    assert t.status == "succeeded" and a.ctx.models.profile("b").seen == []
    await _close(a)


async def test_verify_all_judges_against_the_goal(tmp_path):
    a = await _verified_app(tmp_path, verify_all=True)
    a.ctx.models.profile("b").reply = '{"met": true, "feedback": ""}'
    t, _ = await _run_task(a, done_when=None)
    assert t.status == "succeeded" and len(a.ctx.models.profile("b").seen) == 1
    await _close(a)


async def test_verification_can_be_switched_off(tmp_path):
    a = await _verified_app(tmp_path, verify=False)
    t, _ = await _run_task(a)
    assert t.status == "succeeded" and a.ctx.models.profile("b").seen == []
    await _close(a)


async def test_agent_can_set_done_when_when_starting_a_task(app, chan):
    await chan.say('[[tool:start_task:{"title": "T", "goal": "g", "done_when": "has the price"}]]')
    assert app.ctx.tasks.list()[0].done_when == "has the price"


async def test_chat_budget_is_per_turn_not_per_conversation(tmp_path):
    a = App(make_config(tmp_path, limits={"chat_tool_calls": 1, "repeat_limit": 0}))
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    await ch.say('[[tool:current_time:{}]]')
    await ch.say('[[tool:current_time:{}]]')  # same cached agent; a leaked counter would block this call
    assert "Blocked" not in a.ctx.models.profile("a").prompt_text()
    await a.gateway.stop()
    a.ctx.db.close()


async def test_chat_budget_blocks_within_a_turn(tmp_path):
    a = App(make_config(tmp_path, limits={"chat_tool_calls": 1, "repeat_limit": 0}))
    await a.gateway.start()
    a.ctx.models.profile("a").always_tool = ("current_time", "{}")
    await a.gateway.channels["fake"].say("go")
    assert "budget of 1" in a.ctx.models.profile("a").prompt_text()  # the model was told why
    await a.gateway.stop()
    a.ctx.db.close()
