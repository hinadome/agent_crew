"""Per-task plan + notes: tools, limits, and that the state reaches the model on every call and after a resume."""

from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.tasks.state import render_state, task_state_tools

from .conftest import make_config, wait_for


def _app(tmp_path):
    return App(make_config(tmp_path, tasks={"workers": 1, "max_attempts": 2}))


def _tools(a, tid):
    return {t.tool_name: t for t in task_state_tools(a.ctx, tid)}


def _new(a, title="job"):
    return a.ctx.tasks.create(title, "goal", agent=None, address=None, user_id="u")


def test_plan_tools_keep_status_and_validate(tmp_path):
    a = _app(tmp_path)
    t = _new(a)
    tools = _tools(a, t.id)
    assert "No plan yet" in render_state([], {})
    out = tools["update_plan"](steps=["search flights", "compare prices", "write flights.md"])
    assert "1. [pending] search flights" in out
    assert "2. [done] compare prices - cheapest is KLM" in tools["mark_step"](
        number=2, status="done", note="cheapest is KLM")
    revised = tools["update_plan"](steps=["search flights", "compare prices", "check baggage", "write flights.md"])
    assert "2. [done] compare prices" in revised and "3. [pending] check baggage" in revised  # status kept
    assert "No step 9" in tools["mark_step"](number=9, status="done")
    assert "Unknown status" in tools["mark_step"](number=1, status="finished")
    assert "at least one step" in tools["update_plan"](steps=["  "])
    assert "Too many steps" in tools["update_plan"](steps=[f"s{i}" for i in range(31)])
    assert any("step 2 done" in p for p in a.ctx.tasks.get(t.id).progress)
    fresh = _new(a, "other")
    assert "no plan yet" in _tools(a, fresh.id)["mark_step"](number=1, status="done")
    a.ctx.db.close()


def test_notes_save_delete_and_limits(tmp_path):
    a = _app(tmp_path)
    t = _new(a)
    save = _tools(a, t.id)["save_note"]
    assert save(key="best", value="KLM 210 EUR") == "Saved."
    assert a.ctx.tasks.get(t.id).notes == {"best": "KLM 210 EUR"}
    assert save(key="best", value="") == "Deleted." and a.ctx.tasks.get(t.id).notes == {}
    for i in range(40):
        save(key=f"k{i}", value="v")
    assert "Too many notes" in save(key="one-more", value="v")
    assert save(key="k0", value="updated") == "Saved."  # updating an existing key is fine at the cap
    big = _new(a, "big")
    sb = _tools(a, big.id)["save_note"]
    for i in range(3):
        sb(key=f"b{i}", value="x" * 2000)
    assert "exceed" in sb(key="b4", value="x" * 2000)
    assert "Give the note a key" in sb(key=" ", value="x")
    a.ctx.db.close()


async def test_state_is_in_the_system_prompt_of_every_model_call_and_after_resume(tmp_path):
    a = _app(tmp_path)
    await a.gateway.start()
    a.runner.start()
    model = a.ctx.models.profile("a")
    title = '[[tool:update_plan:{"steps": ["find sources", "write summary"]}]]'
    t = a.ctx.tasks.create(title, "research X", agent=None, address=Address("fake", "c1"), user_id="u")
    await wait_for(lambda: a.ctx.tasks.get(t.id).status == "succeeded")
    systems = [str(s) for s in model.systems]
    assert "No plan yet" in systems[0]  # first call: before the plan existed
    assert "1. [pending] find sources" in systems[-1]  # the call right after update_plan already sees it
    assert "update_plan" in systems[0]  # the task prompt teaches the tools

    # a later attempt (e.g. after a crash) starts with the saved plan in its system prompt
    a.ctx.tasks.set_notes(t.id, {"source": "https://example.org/x"})
    a.ctx.tasks.db.execute("UPDATE tasks SET status='running' WHERE id=?", (t.id,))
    a.ctx.tasks.recover_interrupted()
    n = len(model.systems)
    a.runner.wake()
    await wait_for(lambda: a.ctx.tasks.get(t.id).status == "succeeded" and len(model.systems) > n)
    resumed = str(model.systems[n])
    assert "1. [pending] find sources" in resumed and "source: https://example.org/x" in resumed
    await a.runner.stop()
    await a.gateway.stop()
    a.ctx.db.close()


async def test_chat_agents_do_not_get_task_state(app, chan, models):
    await chan.say("hello")
    assert "task_state" not in app.ctx.models.profile("a").system_text()


async def test_task_status_tool_shows_the_plan(tmp_path):
    a = _app(tmp_path)
    t = _new(a)
    _tools(a, t.id)["update_plan"](steps=["one", "two"])
    from kiro_crew.tasks.tools import _tasks

    status = {x.tool_name: x for x in _tasks(a.ctx)}["task_status"]
    out = status(task_id=t.id)
    assert "Plan:\n1. [pending] one\n2. [pending] two" in out and "plan 0/2" in out
    a.ctx.db.close()
