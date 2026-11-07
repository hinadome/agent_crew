import logging

import pytest

from kiro_crew.core import logging as L


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_kiro", False)]:
        root.removeHandler(h)
    L._RING = None


def test_configure_installs_ring_and_levels():
    r = L.configure("DEBUG", areas={"gateway": "info", "kiro_crew.tasks": "warning"})
    assert L.ring() is r
    assert logging.getLogger("kiro_crew").level == logging.DEBUG
    assert logging.getLogger("kiro_crew.gateway").level == logging.INFO
    assert logging.getLogger("kiro_crew.tasks").level == logging.WARNING
    # Strands prints message content at DEBUG, so it is held at WARNING while content hiding is on...
    assert logging.getLogger("strands").level == logging.WARNING


def test_strands_follows_debug_when_content_logging_enabled():
    L.configure("DEBUG", include_content=True)
    assert logging.getLogger("strands").level == logging.DEBUG


def test_strands_quiet_by_default():
    L.configure("INFO")
    assert logging.getLogger("strands").level == logging.WARNING


def test_reconfigure_replaces_our_handlers_only():
    other = logging.StreamHandler()
    logging.getLogger().addHandler(other)
    try:
        L.configure("INFO")
        n1 = len([h for h in logging.getLogger().handlers if getattr(h, "_kiro", False)])
        L.configure("DEBUG")
        n2 = len([h for h in logging.getLogger().handlers if getattr(h, "_kiro", False)])
        assert n1 == n2  # not duplicated
        assert other in logging.getLogger().handlers  # foreign handler untouched
    finally:
        logging.getLogger().removeHandler(other)


def test_turn_context_tags_records():
    r = L.configure("DEBUG")
    lg = logging.getLogger("kiro_crew.gateway")
    tok = L.start_turn("chat", channel="discord", conversation="c9", user="u7", agent="main")
    L.log(lg, logging.INFO, "hello", foo="bar")
    L.end_turn(tok)
    L.log(lg, logging.INFO, "after")  # outside a turn
    events = r.recent()
    tagged = events[-2]
    assert tagged.turn_id and tagged.user == "u7" and tagged.channel == "discord" and tagged.agent == "main"
    assert tagged.fields.get("foo") == "bar" and "message" not in tagged.fields
    assert events[-1].turn_id is None  # context cleared


def test_log_helper_handles_reserved_field_names():
    """Fields colliding with LogRecord attributes (name, module, ...) must not crash logging."""
    r = L.configure("DEBUG")
    lg = logging.getLogger("kiro_crew.gateway")
    L.log(lg, logging.INFO, "inbound", name="alice", module="x", args=[1], sender="u1")
    e = r.recent()[-1]
    assert e.fields.get("f_name") == "alice" and e.fields.get("sender") == "u1"


def test_area_debug_override_passes_handlers_even_at_info_base():
    """A per-area DEBUG override with an INFO base must actually emit (handler level follows the minimum)."""
    r = L.configure("INFO", areas={"gateway": "debug"})
    logging.getLogger("kiro_crew.gateway").debug("area debug line")
    logging.getLogger("kiro_crew.tasks").debug("other area debug")  # still INFO -> suppressed
    msgs = [e.message for e in r.recent()]
    assert "area debug line" in msgs and "other area debug" not in msgs


def test_quiet_console_keeps_console_at_info_but_ring_gets_debug(capsys):
    r = L.configure("DEBUG", quiet_console=True)
    logging.getLogger("kiro_crew.gateway").debug("deep detail")
    err = capsys.readouterr().err
    assert "deep detail" not in err  # console stayed at INFO
    assert any("deep detail" == e.message for e in r.recent())  # but captured for the web UI
    r = L.configure("INFO")
    lg = logging.getLogger("kiro_crew.gateway")
    L.log(lg, logging.DEBUG, "should not appear")
    assert not any("should not appear" in e.message for e in r.recent())


def test_json_format(capsys):
    import json

    L.configure("INFO", json_format=True)
    logging.getLogger("kiro_crew.gateway").info("hi there")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["message"] == "hi there" and obj["logger"] == "kiro_crew.gateway" and obj["level"] == "INFO"


def test_file_output(tmp_path):
    path = tmp_path / "logs" / "k.log"
    L.configure("INFO", file=path)
    logging.getLogger("kiro_crew.gateway").info("written to file")
    assert path.exists() and "written to file" in path.read_text()


def test_ring_capacity_bounded():
    r = L.configure("DEBUG", ring_capacity=10)
    lg = logging.getLogger("kiro_crew.gateway")
    for i in range(50):
        lg.info("msg %d", i)
    assert len(r.recent(1000)) == 10


def test_recent_min_level_filter():
    r = L.configure("DEBUG")
    lg = logging.getLogger("kiro_crew.gateway")
    lg.debug("d")
    lg.warning("w")
    assert [e.message for e in r.recent(min_level=logging.WARNING)] == ["w"]


def test_parse_area_overrides():
    assert L.parse_area_overrides(["gateway=debug", "memory=info"]) == {"gateway": "debug", "memory": "info"}
    with pytest.raises(ValueError, match="area=level"):
        L.parse_area_overrides(["gatewaydebug"])


def test_invalid_level_rejected():
    with pytest.raises(ValueError, match="log level"):
        L.configure("CHATTY")
