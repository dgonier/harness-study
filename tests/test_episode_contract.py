import textwrap
from pathlib import Path

from harness.contract_tests import run_contract_tests
from harness.episode import Engine, InProcessPolicy
from harness.eventlog import EventLog, read_events
from harness.meta_protocol import MetaProtocol
from harness.team_api import ToolSpec
from harness.team_runtime import TeamRuntime

ROOT = Path(__file__).resolve().parent.parent
STARTER = ROOT / "starter_team_harness"
IMPOSED = ROOT / "harness" / "policies" / "imposed_vote"
AGENTS = ["Agent-1", "Agent-2", "Agent-3"]
TOOLS = [ToolSpec(n, n) for n in ("list_dir", "read_file", "write_file", "run_tests", "submit_final_report")]


def recording_executor(calls):
    def ex(agent, tool, args):
        calls.append((agent, tool, args))
        return f"{tool} done"
    return ex


def test_engine_with_starter_private_visibility(tmp_path):
    calls = []
    log = EventLog(tmp_path / "e.jsonl", "ep")
    seen = {}

    def actor(agent, visible, tools, turn):
        seen.setdefault(agent, []).append([m.text for m in visible])
        turn.say(f"hi from {agent}")
        turn.tool("read_file", {"path": f"{agent}.txt"})
        if agent == "Agent-3":
            turn.tool("submit_final_report", {"status": "incomplete", "summary": "x"})

    with TeamRuntime(STARTER, tmp_path / "state", AGENTS, TOOLS, log=log, phase=2) as rt:
        res = Engine(AGENTS, rt, TOOLS, recording_executor(calls), log=log, phase=2).run(actor)
    log.close()
    assert res.ended_by == "is_done" and res.submitted and res.turns == 3
    assert ("Agent-3", "submit_final_report", {"status": "incomplete", "summary": "x"}) in calls
    # Agent-2 sees Agent-1's message but not Agent-1's private tool call
    agent2_view = seen["Agent-2"][0]
    assert "hi from Agent-1" in agent2_view
    assert not any("Agent-1.txt" in t for t in agent2_view)
    types = [e["type"] for e in read_events(log.path)]
    for t in ("message", "tool_request", "policy_decision", "tool_execution", "tool_result"):
        assert t in types


def test_engine_imposed_vote_release_and_reject(tmp_path):
    calls = []
    log = EventLog(tmp_path / "e.jsonl", "ep")

    def actor(agent, visible, tools, turn):
        if agent == "Agent-1" and turn.engine.round == 1:
            out = turn.tool("write_file", {"path": "a", "content": "b"})
            assert out.startswith("deferred (req-1)")
            out = turn.tool("submit_final_report", {"status": "complete", "summary": "s"})
            assert out.startswith("deferred (req-2)")
        elif agent == "Agent-2" and turn.engine.round == 1:
            turn.tool("vote", {"request_id": "req-1", "approve": True})
            turn.tool("vote", {"request_id": "req-2", "approve": False})
        elif agent == "Agent-3" and turn.engine.round == 1:
            turn.tool("vote", {"request_id": "req-2", "approve": False})

    with TeamRuntime(IMPOSED, tmp_path / "state", AGENTS, TOOLS, log=log, phase=2) as rt:
        res = Engine(AGENTS, rt, TOOLS, recording_executor(calls), log=log, phase=2,
                     max_rounds=2).run(actor)
    log.close()
    assert calls == [("Agent-1", "write_file", {"path": "a", "content": "b"})]
    assert not res.submitted and res.ended_by == "max_rounds"
    decisions = [e["payload"]["action"] for e in read_events(log.path) if e["type"] == "policy_decision"]
    assert decisions == ["defer", "defer", "release", "reject"]


def test_tool_call_cap_and_unknown_tool(tmp_path):
    out = []

    def actor(agent, visible, tools, turn):
        out.extend(turn.tool("read_file", {"path": "x"}) for _ in range(3))
        out.append(turn.tool("rm_rf", {}))

    with TeamRuntime(STARTER, tmp_path / "state", AGENTS[:2], TOOLS) as rt:
        Engine(AGENTS[:2], rt, TOOLS, lambda a, t, x: "ok", max_rounds=1,
               max_tool_calls_per_turn=2).run(actor)
    assert out[:3] == ["ok", "ok", "error: tool call limit for this turn reached"]


def test_policy_cannot_execute_tools(tmp_path):
    """Team code only gets team_api; tool execution happens only via the engine executor."""
    team = tmp_path / "team"
    team.mkdir()
    (team / "policy.py").write_text(textwrap.dedent("""
        from team_api import Decision
        class TeamPolicy:
            def __init__(self, agents, tools, state):
                self.agents, self.state = agents, state
            def next_speaker(self, ctx):
                return self.agents[0]
            def visible_context(self, agent, ctx):
                return []
            def on_tool_request(self, req, ctx):
                import harness.tools.fs
                return Decision("execute")
            def on_message(self, agent, text, ctx): pass
            def extra_tools(self): return []
            def on_extra_tool(self, agent, name, args, ctx): return ""
            def is_done(self, ctx): return True
    """))
    log = EventLog(tmp_path / "e.jsonl", "ep")
    calls = []
    with TeamRuntime(team, tmp_path / "state", AGENTS, TOOLS, log=log) as rt:
        Engine(AGENTS, rt, TOOLS, recording_executor(calls), log=log, max_rounds=1).run(
            lambda a, v, t, turn: turn.tool("write_file", {"path": "x", "content": "y"}))
    log.close()
    assert calls == []  # import of outer-harness code blocked -> deny
    errs = [e["payload"]["kind"] for e in read_events(log.path) if e["type"] == "policy_error"]
    assert "import_blocked" in errs


def test_meta_protocol_all_visible_and_stops_on_ratify(tmp_path):
    state = {"ratified": False}
    seen = []

    def actor(agent, visible, tools, turn):
        seen.append((agent, [m.text for m in visible]))
        turn.tool("write_file", {"path": "team_harness/policy.py", "content": agent})
        if agent == "Agent-2":
            state["ratified"] = True

    mp = InProcessPolicy(MetaProtocol(AGENTS, lambda: state["ratified"]))
    res = Engine(AGENTS, mp, TOOLS, lambda a, t, x: f"{a} wrote", phase=1, max_rounds=5).run(actor)
    assert res.ended_by == "is_done" and res.turns == 2
    assert "Agent-1 wrote" in seen[1][1]  # Agent-2 sees Agent-1's edit result


def test_contract_tests_pass_for_shipped_policies():
    for d in (STARTER, IMPOSED):
        r = run_contract_tests(d, AGENTS, TOOLS)
        assert r.passed, r.summary()


def _policy(tmp_path, body):
    d = tmp_path / "team"
    d.mkdir()
    (d / "policy.py").write_text(STARTER.joinpath("policy.py").read_text().replace(*body))
    return d


def test_contract_fails_never_done(tmp_path):
    d = _policy(tmp_path, ("return ctx.submitted", "return False"))
    r = run_contract_tests(d, AGENTS, TOOLS, max_rounds=3)
    assert not r.passed and not r.checks["is_done_within_max_rounds"][0]


def test_contract_fails_no_execute(tmp_path):
    d = _policy(tmp_path, ('return Decision("execute", visible_to="self")', 'return Decision("deny")'))
    r = run_contract_tests(d, AGENTS, TOOLS, max_rounds=3)
    assert not r.passed and not r.checks["tool_executes"][0]


def test_contract_fails_agent_never_speaks(tmp_path):
    d = _policy(tmp_path, ("return self.agents[turn % len(self.agents)]", "return self.agents[0]"))
    r = run_contract_tests(d, AGENTS, TOOLS, max_rounds=3)
    # Agent-1 submits on its first turn, so the others never get to speak
    assert not r.passed and not r.checks["every_agent_speaks"][0]


def test_contract_fails_on_hook_error(tmp_path):
    d = _policy(tmp_path, ("    def on_message(self, agent, text, ctx):\n        pass",
                           "    def on_message(self, agent, text, ctx):\n        raise ValueError('x')"))
    r = run_contract_tests(d, AGENTS, TOOLS)
    assert not r.passed and not r.checks["no_hook_errors"][0]


def test_contract_fails_unloadable(tmp_path):
    d = tmp_path / "team"
    d.mkdir()
    (d / "policy.py").write_text("class Nope: pass\n")
    r = run_contract_tests(d, AGENTS, TOOLS)
    assert not r.passed and "loads" in r.checks
