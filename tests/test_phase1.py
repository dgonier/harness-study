import json
from pathlib import Path

from harness.config import Phase1Config
from harness.eventlog import EventLog, read_events
from harness.phase1 import STARTER_DIR, run_phase1
from harness.ratify import version_hash
from harness.team_api import ToolSpec

AGENTS = ["Agent-1", "Agent-2", "Agent-3"]
P2_TOOLS = [ToolSpec(n, n) for n in ("list_dir", "read_file", "write_file", "run_tests", "submit_final_report")]
IMPOSED = Path(__file__).resolve().parent.parent / "harness" / "policies" / "imposed_vote" / "policy.py"


def run(tmp_path, actor, **cfg):
    log = EventLog(tmp_path / "ep" / "ground_truth" / "events.jsonl", "ep")
    res = run_phase1(tmp_path / "ep", AGENTS, actor, Phase1Config(**cfg), P2_TOOLS, log)
    log.close()
    return res, list(read_events(log.path))


def designing_actor(new_policy: str):
    """Scripted Phase 1 team: Agent-1 writes a new policy, everyone tests and ratifies.
    In the nested dry run (Phase 2 tools) the same actor just reads and submits."""
    def actor(agent, visible, tools, turn):
        names = {t.name for t in tools}
        if "ratify" not in names:  # inside dry_run
            turn.tool("read_file", {"path": "README.md"})
            turn.tool("submit_final_report", {"status": "complete", "summary": "dry"})
            return
        rnd = turn.engine.round
        if rnd == 1 and agent == "Agent-1":
            turn.say("I propose majority voting for writes.")
            out = turn.tool("write_file", {"path": "team_harness/policy.py", "content": new_policy})
            assert "Current harness version" in out
        elif rnd == 1:
            turn.say("Let me test it.")
            assert "PASS" in turn.tool("run_contract_tests")
            if agent == "Agent-2":
                assert turn.tool("dry_run").startswith("Dry run #1")
        else:
            h = turn.tool("run_contract_tests").rsplit("Harness version: ", 1)[1].strip()
            turn.say(f"ratifying {h}")
            turn.tool("ratify", {"version_hash": h})
    return actor


def test_phase1_edit_test_ratify(tmp_path):
    res, events = run(tmp_path, designing_actor(IMPOSED.read_text()), max_rounds=5)
    assert res.ratified and res.contract_passed and res.fallback_reason is None
    assert res.rounds == 2 and res.ratified_by == AGENTS
    assert (res.harness_dir / "policy.py").read_text() == IMPOSED.read_text()
    assert version_hash(res.harness_dir) == res.final_hash
    gt = tmp_path / "ep" / "ground_truth"
    assert (gt / "versions" / f"{res.final_hash}.diff").read_text().count("+") > 10
    assert (gt / "dryrun_1.jsonl").exists()
    types = [e["type"] for e in events]
    assert types[0] == "phase_start" and types[-1] == "phase_end"
    assert types.count("ratify") == 3 and "file_edit" in types
    final = [json.loads(l) for l in (gt / "versions.jsonl").read_text().splitlines()][-1]
    assert final["ratified"] and final["rejected_versions"] == [res.versions[0]]


def test_phase1_no_ratification_falls_back(tmp_path):
    def actor(agent, visible, tools, turn):
        turn.say("I'm not sure.")
    res, events = run(tmp_path, actor, max_rounds=2)
    assert not res.ratified and "not unanimously ratified" in res.fallback_reason
    assert version_hash(res.harness_dir) == version_hash(STARTER_DIR)
    assert events[-1]["payload"]["fallback"] is True


def test_phase1_ratified_but_broken_falls_back(tmp_path):
    broken = STARTER_DIR.joinpath("policy.py").read_text().replace("return ctx.submitted", "return False")
    res, _ = run(tmp_path, designing_actor_no_check(broken), max_rounds=4)
    assert res.ratified and not res.contract_passed
    assert "contract tests" in res.fallback_reason
    assert version_hash(res.harness_dir) == version_hash(STARTER_DIR)


def designing_actor_no_check(new_policy):
    """Agent-1 writes a policy in round 1; everyone ratifies it in round 2 without testing."""
    def actor(agent, visible, tools, turn):
        eng = turn.engine
        if eng.round == 1 and agent == "Agent-1":
            out = turn.tool("write_file", {"path": "team_harness/policy.py", "content": new_policy})
            eng._h = out.rsplit(": ", 1)[1]
        elif eng.round == 2:
            turn.tool("ratify", {"version_hash": eng._h})
    return actor


def test_phase1_edit_voids_ratification(tmp_path):
    def actor(agent, visible, tools, turn):
        eng = turn.engine
        if agent == "Agent-1" and eng.round == 1:
            out = turn.tool("write_file", {"path": "team_harness/notes.md", "content": "v1"})
            eng._h1 = out.rsplit(": ", 1)[1]
            turn.tool("ratify", {"version_hash": eng._h1})
        elif agent == "Agent-2" and eng.round == 1:
            turn.tool("ratify", {"version_hash": eng._h1})
        elif agent == "Agent-3" and eng.round == 1:
            out = turn.tool("write_file", {"path": "team_harness/notes.md", "content": "v2"})
            eng._h2 = out.rsplit(": ", 1)[1]
            assert "not the current version" in turn.tool("ratify", {"version_hash": eng._h1})
    res, _ = run(tmp_path, actor, max_rounds=2)
    assert not res.ratified and len(res.versions) == 3


def test_phase1_sandbox_scoping(tmp_path):
    outs = {}

    def actor(agent, visible, tools, turn):
        if agent != "Agent-1" or turn.engine.round > 1:
            return
        outs["docs_write"] = turn.tool("write_file", {"path": "docs/team_api.md", "content": "x"})
        outs["escape"] = turn.tool("read_file", {"path": "../ground_truth/events.jsonl"})
        outs["docs_read"] = turn.tool("read_file", {"path": "docs/team_api.md"})
        outs["ls"] = turn.tool("list_dir", {"path": "."})
    run(tmp_path, actor, max_rounds=1)
    assert outs["docs_write"].startswith("error") and outs["escape"].startswith("error")
    assert outs["docs_read"].startswith("# Team harness")
    assert outs["ls"] == "docs/\nteam_harness/"
