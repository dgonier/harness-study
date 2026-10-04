"""Contract tests a team harness must pass before Phase 2.

They check only that the harness *works*, not that it is safe, fair or
strict:

1. On a scripted standard episode, every agent speaks at least once.
2. At least one tool request reaches `execute` (directly or via release).
3. `is_done` returns True within `max_rounds` once a final report is
   accepted.
4. No hook raises, times out, returns something invalid, or trips a sandbox
   guard during that episode.

The scripted episode uses cooperative actors: each turn an agent posts a
message, reads a file, and requests the submit tool. If the team harness
defers requests, actors call any team-defined tool that takes a
`request_id` (passing True for its boolean fields), which is how simple
voting/approval schemes get exercised. Tool execution is stubbed: no
workspace is touched.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .episode import Engine, EpisodeResult
from .eventlog import EventLog, read_events
from .team_api import ToolSpec
from .team_runtime import TeamRuntime

SUBMIT = "submit_final_report"


@dataclass
class ContractResult:
    passed: bool
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)
    episode: EpisodeResult | None = None

    def summary(self) -> str:
        lines = ["CONTRACT TESTS: " + ("PASS" if self.passed else "FAIL")]
        for name, (ok, detail) in self.checks.items():
            lines.append(f"  [{'x' if ok else ' '}] {name}: {detail}")
        return "\n".join(lines)


def _stub_executor(agent: str, tool: str, args: dict) -> str:
    return f"[contract test stub] {tool} ok"


def _approval_args(spec: ToolSpec, request_id: str) -> dict | None:
    props = (spec.parameters or {}).get("properties", {})
    if "request_id" not in props:
        return None
    args = {"request_id": request_id}
    for name, schema in props.items():
        if name != "request_id" and isinstance(schema, dict) and schema.get("type") == "boolean":
            args[name] = True
    return args


def cooperative_actor(agent, visible, tools, turn):
    turn.say(f"{agent} checking in.")
    turn.tool("read_file", {"path": "README.md"})
    # approve anything still pending, using team-defined approval tools
    pending = turn.engine.ctx().pending
    for req in pending:
        if req.agent == agent:
            continue
        for spec in turn.engine.extra:
            args = _approval_args(spec, req.request_id)
            if args is not None:
                turn.tool(spec.name, args)
    everyone_spoke = all(n > 0 for n in turn.engine.spoke.values())
    if everyone_spoke and not turn.engine.submitted and not any(
            r.tool == SUBMIT for r in turn.engine.ctx().pending):
        turn.tool(SUBMIT, {"status": "complete", "summary": "contract test"})


def run_contract_tests(
    team_dir: str | Path,
    agents: list[str],
    tools: list[ToolSpec],
    *,
    max_rounds: int = 15,
    timeout_s: float = 2.0,
    log: EventLog | None = None,
) -> ContractResult:
    tool_names = {t.name for t in tools}
    missing = {"read_file", SUBMIT} - tool_names
    if missing:
        raise ValueError(f"contract tests need tools {sorted(missing)}")
    with tempfile.TemporaryDirectory(prefix="contract_") as tmp:
        tmp = Path(tmp)
        with EventLog(tmp / "events.jsonl", "contract") as own_log:
            with TeamRuntime(team_dir, tmp / "state", agents, tools, timeout_s=timeout_s,
                             log=own_log, phase=0) as rt:
                if not rt.healthy:
                    errs = [e["payload"] for e in read_events(own_log.path) if e["type"] == "policy_error"]
                    detail = errs[0].get("detail", "") if errs else "unknown"
                    return ContractResult(False, {"loads": (False, f"policy failed to load: {detail}")})
                engine = Engine(agents, rt, tools, _stub_executor, log=own_log, phase=0,
                                max_rounds=max_rounds, submit_tool=SUBMIT)
                result = engine.run(cooperative_actor)
            errors = [e["payload"] for e in read_events(own_log.path) if e["type"] == "policy_error"]
        if log is not None:
            log.emit("policy_error" if errors else "phase_end",
                     {"contract_tests": True, "errors": errors[:20]}, phase=1)

    silent = [a for a, n in result.spoke.items() if n == 0]
    checks = {
        "loads": (True, "TeamPolicy loaded"),
        "every_agent_speaks": (not silent, "all agents spoke" if not silent else f"never spoke: {silent}"),
        "tool_executes": (result.executed > 0, f"{result.executed} tool requests executed"),
        "is_done_within_max_rounds": (
            result.ended_by in ("is_done", "next_speaker_none") and result.submitted,
            f"ended_by={result.ended_by}, submitted={result.submitted}, rounds={result.rounds}"),
        "no_hook_errors": (not errors, "no errors" if not errors else
                           "; ".join(f"{e.get('hook')}: {e.get('kind')} {str(e.get('detail',''))[:80]}"
                                     for e in errors[:5])),
    }
    return ContractResult(all(ok for ok, _ in checks.values()), checks, result)
