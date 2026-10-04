import json

import pytest

from harness.eventlog import EventLog, read_events
from harness.team_api import Context, Decision, Message, StateStore, ToolRequest


def test_append_only_and_schema(tmp_path):
    p = tmp_path / "ep" / "events.jsonl"
    with EventLog(p, "ep1") as log:
        log.emit("phase_start", {"condition": "unilateral"}, phase=2)
        log.emit("message", {"text": "hi"}, phase=2, round=1, agent="Agent-1", model="fake")
    with EventLog(p, "ep1") as log:  # reopen must not truncate
        log.emit("phase_end", phase=2)
    events = list(read_events(p))
    assert [e["type"] for e in events] == ["phase_start", "message", "phase_end"]
    for e in events:
        assert set(e) >= {"ts", "episode", "phase", "round", "agent", "model", "type", "payload"}


def test_unknown_type_rejected(tmp_path):
    with EventLog(tmp_path / "e.jsonl", "ep") as log:
        with pytest.raises(ValueError):
            log.emit("bogus")


def test_decision_validation():
    Decision("execute", visible_to="all")
    Decision("defer", visible_to=["Agent-1"])
    with pytest.raises(ValueError):
        Decision("maybe")
    with pytest.raises(ValueError):
        Decision("deny", visible_to="team")


def test_context_roundtrip_and_commands():
    ctx = Context(
        round=2, phase=2, agents=["Agent-1", "Agent-2"],
        history=[Message("Agent-1", "hello", 1)],
        pending=[ToolRequest("r1", "Agent-2", "run_tests", {})],
    )
    ctx.release("r1")
    ctx.reject("r2", "no quorum")
    assert [c["op"] for c in ctx.commands()] == ["release", "reject"]
    d = json.loads(json.dumps(ctx.to_dict()))
    ctx2 = Context.from_dict(d)
    assert ctx2.history[0].text == "hello"
    assert ctx2.pending[0].tool == "run_tests"
    assert ctx2.commands() == []


def test_state_store():
    s = StateStore()
    s.set("votes", {"r1": ["Agent-1"]})
    assert s.get("votes")["r1"] == ["Agent-1"]
    s.delete("votes")
    assert s.get("votes") is None
