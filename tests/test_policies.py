from pathlib import Path

import pytest

from harness.eventlog import EventLog, read_events
from harness.team_api import Context, Message, ToolRequest, ToolSpec
from harness.team_runtime import TeamRuntime

ROOT = Path(__file__).resolve().parent.parent
STARTER = ROOT / "starter_team_harness"
IMPOSED = ROOT / "harness" / "policies" / "imposed_vote"
AGENTS = ["Agent-1", "Agent-2", "Agent-3"]
TOOLS = [ToolSpec(n, n) for n in ("list_dir", "read_file", "write_file", "run_tests", "submit_final_report")]


def ctx(history=None, pending=None, submitted=False):
    return Context(round=1, phase=2, agents=AGENTS, history=history or [], pending=pending or [],
                   submitted=submitted)


@pytest.fixture
def log(tmp_path):
    with EventLog(tmp_path / "logs" / "e.jsonl", "ep") as lg:
        yield lg


def no_errors(log):
    assert [e for e in read_events(log.path) if e["type"] == "policy_error"] == []


def test_message_visibility():
    m = Message("Agent-1", "x", visible_to=["Agent-2"])
    assert m.visible_for("Agent-1") and m.visible_for("Agent-2") and not m.visible_for("Agent-3")
    assert not Message("Agent-1", "x", visible_to="self").visible_for("Agent-2")


def test_starter_policy(tmp_path, log):
    hist = [Message("Agent-1", "hello"),
            Message("Agent-2", "read_file(x)", kind="tool_call", visible_to=["Agent-2"], request_id="r0")]
    with TeamRuntime(STARTER, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert rt.healthy
        assert [rt.next_speaker(ctx())[0] for _ in range(4)] == AGENTS + ["Agent-1"]
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-3", "write_file", {}), ctx())
        assert (d.action, d.visible_to) == ("execute", "self")
        assert len(rt.visible_context("Agent-1", ctx(hist))[0]) == 1
        assert len(rt.visible_context("Agent-2", ctx(hist))[0]) == 2
        assert rt.extra_tools() == []
        assert not rt.is_done(ctx()) and rt.is_done(ctx(submitted=True))
    no_errors(log)


def test_imposed_vote_free_tools_execute(tmp_path, log):
    with TeamRuntime(IMPOSED, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        for tool in ("list_dir", "read_file", "run_tests"):
            d, _ = rt.on_tool_request(ToolRequest("r", "Agent-1", tool, {}), ctx())
            assert (d.action, d.visible_to) == ("execute", "self")
    no_errors(log)


def test_imposed_vote_approval(tmp_path, log):
    req = ToolRequest("r1", "Agent-1", "write_file", {"path": "src/a.py"}, rationale="fix")
    with TeamRuntime(IMPOSED, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert [t.name for t in rt.extra_tools()] == ["vote"]
        d, _ = rt.on_tool_request(req, ctx())
        assert (d.action, d.visible_to) == ("defer", "all")
        notice = rt.visible_context("Agent-3", ctx(pending=[req]))[0][-1]
        assert notice.kind == "notice" and "r1" in notice.text
        out, cmds = rt.on_extra_tool("Agent-1", "vote", {"request_id": "r1", "approve": True}, ctx())
        assert "already voted" in out and cmds == []
        out, cmds = rt.on_extra_tool("Agent-2", "vote", {"request_id": "r1", "approve": True}, ctx())
        assert "approved" in out
        assert cmds == [{"op": "release", "request_id": "r1"}]
        out, _ = rt.on_extra_tool("Agent-3", "vote", {"request_id": "r1", "approve": False}, ctx())
        assert "already approved" in out
        assert rt.visible_context("Agent-3", ctx())[0] == []  # no open proposals left
    no_errors(log)


def test_imposed_vote_rejection(tmp_path, log):
    with TeamRuntime(IMPOSED, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        rt.on_tool_request(ToolRequest("r1", "Agent-1", "submit_final_report", {"status": "complete"}), ctx())
        _, cmds = rt.on_extra_tool("Agent-2", "vote", {"request_id": "r1", "approve": False}, ctx())
        assert cmds == []  # 1 yes, 1 no: still reachable
        _, cmds = rt.on_extra_tool("Agent-3", "vote", {"request_id": "r1", "approve": False}, ctx())
        assert cmds[0]["op"] == "reject" and cmds[0]["request_id"] == "r1"
        out, _ = rt.on_extra_tool("Agent-2", "vote", {"request_id": "nope", "approve": True}, ctx())
        assert "no proposal" in out
    no_errors(log)


def test_imposed_vote_two_agent_team_needs_both(tmp_path, log):
    agents = AGENTS[:2]
    with TeamRuntime(IMPOSED, tmp_path / "state", agents, TOOLS, log=log) as rt:
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "write_file", {}), ctx())
        assert d.action == "defer"
        _, cmds = rt.on_extra_tool("Agent-2", "vote", {"request_id": "r1", "approve": True}, ctx())
        assert cmds[0]["op"] == "release"
    no_errors(log)
