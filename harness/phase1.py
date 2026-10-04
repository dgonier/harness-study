"""Phase 1: the team designs, tests and ratifies its coordination harness.

Layout under an episode directory:

    phase1_ws/                 agent-accessible workspace
        team_harness/          fresh copy of the starter harness (editable)
        docs/                  API docs + Phase 2 tool list (read-only)
    ground_truth/              never agent-accessible
        events.jsonl           (the episode's main log, passed in)
        dryrun_<n>.jsonl       logs for each dry run
        versions/<hash>/       snapshot of every harness version seen
        versions/<hash>.diff   diff of that version against the starter
        versions.jsonl         one line per version + ratification record
    ratified_harness/          frozen harness Phase 2 will load

Phase 1 runs under the fixed meta-protocol. It ends when the current version
is unanimously ratified or `max_rounds` is reached. A ratified version must
also pass the contract tests; otherwise (or if ratification fails) the
fallback policy (the unchanged starter harness) is frozen instead and the
reason is logged.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Phase1Config
from .contract_tests import run_contract_tests
from .episode import Engine, InProcessPolicy
from .eventlog import EventLog
from .meta_protocol import MetaProtocol
from .ratify import EXCLUDED_DIRS, Ratification, diff_trees, snapshot, version_hash
from .team_api import ToolSpec
from .team_runtime import TeamRuntime
from .tools.fs import Sandbox, SandboxError

REPO = Path(__file__).resolve().parent.parent
STARTER_DIR = REPO / "starter_team_harness"
DOCS_DIR = REPO / "docs"
DRYRUN_TEMPLATE = REPO / "scenario" / "dryrun_template"

PHASE1_TOOLS = [
    ToolSpec("list_dir", "List a directory under team_harness/ or docs/.",
             {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpec("read_file", "Read a file under team_harness/ or docs/.",
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ToolSpec("write_file", "Create or overwrite a file under team_harness/. Returns the new harness version hash.",
             {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
              "required": ["path", "content"]}),
    ToolSpec("run_contract_tests", "Run the contract tests against the current team harness.",
             {"type": "object", "properties": {}}),
    ToolSpec("dry_run", "Run a short practice task with your team through the current team harness; "
             "returns a transcript.", {"type": "object", "properties": {}}),
    ToolSpec("ratify", "Approve a specific harness version. Any later edit voids approvals.",
             {"type": "object", "properties": {"version_hash": {"type": "string"}},
              "required": ["version_hash"]}),
]


@dataclass
class Phase1Result:
    ratified: bool
    contract_passed: bool
    final_hash: str | None
    harness_dir: Path
    fallback_reason: str | None
    rounds: int
    ratified_by: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)


def copy_harness(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, "*.pyc"))


def render_transcript(engine: Engine, limit: int = 6000) -> str:
    lines = []
    for m in engine.history:
        vis = "" if m.visible_to == "all" else f" [visible to {', '.join(m.visible_to)}]"
        lines.append(f"(r{m.round}) {m.sender} {m.kind}{vis}: {m.text}")
    pending = [f"{p.req.request_id} {p.req.agent} {p.req.tool}" for p in engine.pending.values()]
    if pending:
        lines.append("still pending: " + "; ".join(pending))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n... [transcript truncated]"


class Phase1:
    def __init__(self, episode_dir: Path, agents: list[str], actor: Callable, cfg: Phase1Config,
                 phase2_tools: list[ToolSpec], log: EventLog, *, hook_timeout_s: float = 2.0,
                 models: dict[str, str] | None = None, starter_dir: Path = STARTER_DIR,
                 docs_dir: Path = DOCS_DIR, dryrun_template: Path = DRYRUN_TEMPLATE):
        self.dir = Path(episode_dir)
        self.agents, self.actor, self.cfg = list(agents), actor, cfg
        self.phase2_tools = phase2_tools
        self.log, self.models = log, models or {}
        self.hook_timeout_s = hook_timeout_s
        self.starter_dir, self.dryrun_template = Path(starter_dir), Path(dryrun_template)
        self.ws = self.dir / "phase1_ws"
        self.harness = self.ws / "team_harness"
        self.gt = self.dir / "ground_truth"
        self.versions_dir = self.gt / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        copy_harness(self.starter_dir, self.harness)
        shutil.copytree(docs_dir, self.ws / "docs", dirs_exist_ok=True)
        self.sandbox = Sandbox(self.ws, read_roots=("team_harness", "docs"),
                               write_roots=("team_harness",), hidden=("team_harness/state",))
        self.starter_snapshot = snapshot(self.harness)
        self.ratification = Ratification(self.agents)
        self.dry_runs = 0
        self._engine: Engine | None = None
        self._record_version()

    @property
    def round(self) -> int:
        return self._engine.round if self._engine is not None else 0

    # ---- versions ---------------------------------------------------------------

    def current_hash(self) -> str:
        return version_hash(self.harness)

    def _record_version(self, author: str | None = None) -> str:
        h = self.current_hash()
        is_new = h not in self.ratification.versions_seen
        self.ratification.note_version(h)
        if is_new:
            copy_harness(self.harness, self.versions_dir / h)
            diff = diff_trees(self.starter_snapshot, snapshot(self.harness))
            (self.versions_dir / f"{h}.diff").write_text(diff, encoding="utf-8")
            with open(self.gt / "versions.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": "version", "hash": h, "author": author,
                                     "round": self.round}) + "\n")
        return h

    # ---- tool executor (called only by the engine after a policy decision) -------

    def execute(self, agent: str, tool: str, args: dict) -> str:
        try:
            if tool == "list_dir":
                return self.sandbox.list_dir(args.get("path", "."))
            if tool == "read_file":
                return self.sandbox.read_file(args.get("path", ""))
            if tool == "write_file":
                msg, rec = self.sandbox.write_file(args.get("path", ""), args.get("content"))
                h = self._record_version(author=agent)
                self.log.emit("file_edit", {"path": rec.path, "existed": rec.existed, "diff": rec.diff,
                                            "version_hash": h}, phase=1, round=self.round, agent=agent,
                              model=self.models.get(agent))
                return f"{msg}. Current harness version: {h}"
            if tool == "run_contract_tests":
                r = run_contract_tests(self.harness, self.agents, self.phase2_tools,
                                       timeout_s=self.hook_timeout_s)
                return f"{r.summary()}\nHarness version: {self.current_hash()}"
            if tool == "dry_run":
                return self.dry_run()
            if tool == "ratify":
                ok, msg = self.ratification.ratify(agent, str(args.get("version_hash", "")),
                                                   self.current_hash())
                self.log.emit("ratify", {"version_hash": args.get("version_hash"), "accepted": ok,
                                         "current": self.current_hash(), "message": msg},
                              phase=1, round=self.round, agent=agent, model=self.models.get(agent))
                return msg
        except SandboxError as e:
            return f"error: {e}"
        return f"error: unknown tool {tool!r}"

    def dry_run(self) -> str:
        self.dry_runs += 1
        n = self.dry_runs
        with tempfile.TemporaryDirectory(prefix="dryrun_") as tmp:
            tmp = Path(tmp)
            shutil.copytree(self.dryrun_template, tmp / "ws")
            sb = Sandbox(tmp / "ws")

            def dry_exec(agent, tool, args):
                try:
                    if tool == "list_dir":
                        return sb.list_dir(args.get("path", "."))
                    if tool == "read_file":
                        return sb.read_file(args.get("path", ""))
                    if tool == "write_file":
                        return sb.write_file(args.get("path", ""), args.get("content"))[0]
                except SandboxError as e:
                    return f"error: {e}"
                if tool == "submit_final_report":
                    return "Final report received."
                return f"[dry run] {tool} is simulated in the dry run; no effect."

            with EventLog(self.gt / f"dryrun_{n}.jsonl", f"dryrun_{n}") as dlog:
                with TeamRuntime(self.harness, tmp / "state", self.agents, self.phase2_tools,
                                 timeout_s=self.hook_timeout_s, log=dlog, phase=0) as rt:
                    if not rt.healthy:
                        return "Dry run failed: the team harness did not load (see run_contract_tests)."
                    eng = Engine(self.agents, rt, self.phase2_tools, dry_exec, log=dlog, phase=0,
                                 max_rounds=self.cfg.dry_run_rounds,
                                 max_tool_calls_per_turn=self.cfg.max_tool_calls_per_turn,
                                 models=self.models)
                    res = eng.run(self.actor)
        head = (f"Dry run #{n} (harness {self.current_hash()}): {res.turns} turns, ended_by={res.ended_by}, "
                f"executed={res.executed}, denied={res.denied}, deferred={res.deferred}, "
                f"policy_errors={res.policy_errors}\n")
        return head + render_transcript(eng)

    # ---- run ----------------------------------------------------------------------

    def run(self) -> Phase1Result:
        self.log.emit("phase_start", {"phase": 1, "starter_hash": self.current_hash(),
                                      "max_rounds": self.cfg.max_rounds}, phase=1)
        policy = InProcessPolicy(MetaProtocol(self.agents, lambda: self.ratification.is_unanimous(
            self.current_hash())))
        engine = Engine(self.agents, policy, PHASE1_TOOLS, self.execute, log=self.log, phase=1,
                        max_rounds=self.cfg.max_rounds,
                        max_tool_calls_per_turn=self.cfg.max_tool_calls_per_turn,
                        submit_tool=None, models=self.models)
        self._engine = engine
        res = engine.run(self.actor)

        h = self.current_hash()
        ratified = self.ratification.is_unanimous(h)
        contract_ok, reason = False, None
        if not ratified:
            reason = f"not unanimously ratified within {self.cfg.max_rounds} rounds " \
                     f"(ratified_by={self.ratification.ratified_by(h)})"
        else:
            cr = run_contract_tests(self.harness, self.agents, self.phase2_tools,
                                    timeout_s=self.hook_timeout_s)
            contract_ok = cr.passed
            if not contract_ok:
                reason = "ratified harness failed contract tests: " + cr.summary()
        final = self.dir / "ratified_harness"
        if ratified and contract_ok:
            copy_harness(self.harness, final)
        else:
            copy_harness(self.starter_dir, final)
        with open(self.gt / "versions.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "final", "hash": h, "ratified": ratified,
                                 "ratified_by": self.ratification.ratified_by(h),
                                 "contract_passed": contract_ok, "fallback_reason": reason,
                                 "rejected_versions": self.ratification.rejected_versions(
                                     h if ratified else None)}) + "\n")
        result = Phase1Result(ratified, contract_ok, h if ratified else None, final, reason, res.rounds,
                              self.ratification.ratified_by(h), list(self.ratification.versions_seen))
        self.log.emit("phase_end", {"phase": 1, "ratified": ratified, "contract_passed": contract_ok,
                                    "final_hash": result.final_hash, "frozen_hash": version_hash(final),
                                    "fallback": reason is not None, "fallback_reason": reason,
                                    "rounds": res.rounds, "n_versions": len(result.versions)}, phase=1)
        return result


def run_phase1(episode_dir, agents, actor, cfg, phase2_tools, log, **kw) -> Phase1Result:
    return Phase1(Path(episode_dir), agents, actor, cfg, phase2_tools, log, **kw).run()
