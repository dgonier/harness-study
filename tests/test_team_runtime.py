import textwrap

import pytest

from harness.eventlog import EventLog, read_events
from harness.team_api import Context, Message, ToolRequest, ToolSpec
from harness.team_runtime import SKIP, TeamRuntime

AGENTS = ["Agent-1", "Agent-2", "Agent-3"]
TOOLS = [ToolSpec("read_file", "Read a file"), ToolSpec("run_tests", "Run tests")]

GOOD_POLICY = """
from team_api import Decision, Message, ToolSpec

class TeamPolicy:
    def __init__(self, agents, tools, state):
        self.agents, self.tools, self.state = agents, tools, state
    def next_speaker(self, ctx):
        n = self.state.get("n", 0)
        self.state.set("n", n + 1)
        return self.agents[n % len(self.agents)]
    def visible_context(self, agent, ctx):
        return ctx.history
    def on_tool_request(self, req, ctx):
        if req.tool == "run_tests":
            return Decision("defer", reason="needs a vote")
        return Decision("execute", visible_to="all")
    def on_message(self, agent, text, ctx):
        if text.startswith("approve "):
            ctx.release(text.split()[1])
    def extra_tools(self):
        return [ToolSpec("vote", "Cast a vote", {"type": "object"})]
    def on_extra_tool(self, agent, name, args, ctx):
        return f"{agent} voted {args.get('choice')}"
    def is_done(self, ctx):
        return ctx.round >= 3
"""


def make_policy(tmp_path, body, name="team"):
    d = tmp_path / name
    d.mkdir()
    (d / "policy.py").write_text(textwrap.dedent(body))
    return d


def ctx(round=1, history=None):
    return Context(round=round, phase=2, agents=AGENTS,
                   history=history or [Message("Agent-1", "hi", 1), Message("Agent-2", "yo", 1)],
                   pending=[])


@pytest.fixture
def log(tmp_path):
    with EventLog(tmp_path / "logs" / "events.jsonl", "ep") as lg:
        yield lg


def errors(log):
    return [e for e in read_events(log.path) if e["type"] == "policy_error"]


def test_good_policy_roundtrip(tmp_path, log):
    team = make_policy(tmp_path, GOOD_POLICY)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert rt.healthy
        assert [rt.next_speaker(ctx())[0] for _ in range(4)] == AGENTS + ["Agent-1"]
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {"path": "x"}), ctx())
        assert d.action == "execute" and d.visible_to == "all"
        d, _ = rt.on_tool_request(ToolRequest("r2", "Agent-1", "run_tests", {}), ctx())
        assert d.action == "defer"
        assert rt.on_message("Agent-2", "approve r2", ctx()) == [{"op": "release", "request_id": "r2"}]
        assert [t.name for t in rt.extra_tools()] == ["vote"]
        assert rt.on_extra_tool("Agent-3", "vote", {"choice": "yes"}, ctx())[0] == "Agent-3 voted yes"
        assert len(rt.visible_context("Agent-1", ctx())[0]) == 2
        assert not rt.is_done(ctx(round=1)) and rt.is_done(ctx(round=3))
    assert errors(log) == []


RAISING_POLICY = GOOD_POLICY.replace(
    "    def on_tool_request(self, req, ctx):\n",
    "    def on_tool_request(self, req, ctx):\n        raise RuntimeError('boom')\n",
).replace(
    "    def next_speaker(self, ctx):\n",
    "    def next_speaker(self, ctx):\n        if ctx.round == 2: raise KeyError('x')\n",
)


def test_raising_hook_falls_back_and_continues(tmp_path, log):
    team = make_policy(tmp_path, RAISING_POLICY)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())
        assert d.action == "deny"
        assert rt.next_speaker(ctx(round=2))[0] == SKIP
        assert rt.next_speaker(ctx(round=1))[0] == "Agent-1"  # episode continues
    kinds = [e["payload"]["kind"] for e in errors(log)]
    assert kinds == ["exception", "exception"]


SLOW_POLICY = GOOD_POLICY.replace(
    "    def on_tool_request(self, req, ctx):\n",
    "    def on_tool_request(self, req, ctx):\n        while True: pass\n",
)


def test_timeout_falls_back_and_respawns_with_state(tmp_path, log):
    team = make_policy(tmp_path, SLOW_POLICY)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log, timeout_s=0.5) as rt:
        assert rt.next_speaker(ctx())[0] == "Agent-1"
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())
        assert d.action == "deny"
        # respawned worker resumes from last good state
        assert rt.next_speaker(ctx())[0] == "Agent-2"
    assert [e["payload"]["kind"] for e in errors(log)] == ["timeout"]


@pytest.mark.parametrize("snippet,kind", [
    ("import os", "import_blocked"),
    ("import socket", "import_blocked"),
    ("import subprocess", "import_blocked"),
    ("import importlib", "import_blocked"),
    ("open('/tmp/escape.txt', 'w').write('x')", "fs_blocked"),
    ("open('../outside.txt', 'w').write('x')", "fs_blocked"),
    ("import dataclasses; dataclasses.sys.modules['os'].system('true')", "os_blocked"),
    ("import json; json.codecs.open('/etc/hostname').read()", "fs_blocked"),
    ("eval('1+1')", "builtin_blocked"),
])
def test_sandbox_blocks_and_logs(tmp_path, log, snippet, kind):
    body = GOOD_POLICY.replace(
        "    def on_tool_request(self, req, ctx):\n",
        f"    def on_tool_request(self, req, ctx):\n        {snippet}\n",
    )
    team = make_policy(tmp_path, body)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())
        assert d.action == "deny"
    assert kind in [e["payload"]["kind"] for e in errors(log)]
    assert not (tmp_path / "outside.txt").exists()


def test_caught_violation_is_still_logged(tmp_path, log):
    body = GOOD_POLICY.replace(
        "    def on_tool_request(self, req, ctx):\n",
        "    def on_tool_request(self, req, ctx):\n"
        "        try:\n            import socket\n        except ImportError:\n            pass\n",
    )
    team = make_policy(tmp_path, body)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())
        assert d.action == "execute"  # policy swallowed the error and carried on
    assert [e["payload"]["kind"] for e in errors(log)] == ["import_blocked"]


def test_state_dir_writes_allowed(tmp_path, log):
    body = GOOD_POLICY.replace(
        "    def on_tool_request(self, req, ctx):\n",
        "    def on_tool_request(self, req, ctx):\n"
        "        with open('audit.txt', 'a') as f: f.write(req.tool + '\\n')\n",
    )
    team = make_policy(tmp_path, body)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        d, _ = rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())
        assert d.action == "execute"
    assert (tmp_path / "state" / "audit.txt").read_text() == "read_file\n"
    assert errors(log) == []


def test_bad_returns_fall_back(tmp_path, log):
    body = GOOD_POLICY.replace(
        "        return self.agents[n % len(self.agents)]", "        return 'Agent-99'"
    ).replace(
        "            return Decision(\"defer\", reason=\"needs a vote\")", "            return 'yes please'"
    )
    team = make_policy(tmp_path, body)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert rt.next_speaker(ctx())[0] == SKIP
        assert rt.on_tool_request(ToolRequest("r1", "Agent-1", "run_tests", {}), ctx())[0].action == "deny"
    assert [e["payload"]["kind"] for e in errors(log)] == ["bad_return", "bad_return"]


def test_broken_policy_file_is_unhealthy(tmp_path, log):
    team = make_policy(tmp_path, "this is not python(")
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert not rt.healthy
        assert rt.next_speaker(ctx())[0] == SKIP
        assert rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())[0].action == "deny"
        assert rt.is_done(ctx()) is False
    assert errors(log)[0]["payload"]["hook"] == "__init__"


def test_team_print_does_not_corrupt_protocol(tmp_path, log):
    body = GOOD_POLICY.replace(
        "    def on_tool_request(self, req, ctx):\n",
        "    def on_tool_request(self, req, ctx):\n        print('debug', req.tool)\n",
    )
    team = make_policy(tmp_path, body)
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        assert rt.on_tool_request(ToolRequest("r1", "Agent-1", "read_file", {}), ctx())[0].action == "execute"
    assert errors(log) == []
