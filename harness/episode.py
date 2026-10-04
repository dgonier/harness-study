"""Turn engine shared by Phase 1, Phase 2, the dry run and the contract tests.

The engine asks the coordination policy who speaks, what they see, and what
happens to each tool request; it then executes decisions through an injected
executor and logs everything to the ground-truth EventLog. It never lets the
policy execute anything itself.

An *actor* drives one agent's turn. Real agents (LLM-backed) and scripted
test agents implement the same callable:

    actor(agent: str, visible: list[Message], tools: list[ToolSpec], turn: TurnHandle) -> None

and act only through `turn.say(text)` and `turn.tool(name, args, rationale)`.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .eventlog import EventLog
from .team_api import Context, Decision, Message, ToolRequest, ToolSpec
from .team_runtime import SKIP

Executor = Callable[[str, str, dict], str]  # (agent, tool, args) -> result text


class PolicyHost(Protocol):
    """Tuple-returning hook interface (TeamRuntime, or InProcessPolicy below)."""
    error_count: int

    def next_speaker(self, ctx: Context) -> tuple[str | None, list[dict]]: ...
    def visible_context(self, agent: str, ctx: Context) -> tuple[list[Message], list[dict]]: ...
    def on_tool_request(self, req: ToolRequest, ctx: Context) -> tuple[Decision, list[dict]]: ...
    def on_message(self, agent: str, text: str, ctx: Context) -> list[dict]: ...
    def extra_tools(self) -> list[ToolSpec]: ...
    def on_extra_tool(self, agent: str, name: str, args: dict, ctx: Context) -> tuple[str, list[dict]]: ...
    def is_done(self, ctx: Context) -> bool: ...


class InProcessPolicy:
    """Adapts a trusted, in-process TeamPolicy-style object (e.g. the Phase 1
    meta-protocol) to the PolicyHost interface. Not for team-written code."""

    def __init__(self, policy: Any):
        self.policy = policy
        self.error_count = 0

    def next_speaker(self, ctx):
        return self.policy.next_speaker(ctx), ctx.commands()

    def visible_context(self, agent, ctx):
        return self.policy.visible_context(agent, ctx), ctx.commands()

    def on_tool_request(self, req, ctx):
        return self.policy.on_tool_request(req, ctx), ctx.commands()

    def on_message(self, agent, text, ctx):
        self.policy.on_message(agent, text, ctx)
        return ctx.commands()

    def extra_tools(self):
        return self.policy.extra_tools()

    def on_extra_tool(self, agent, name, args, ctx):
        return str(self.policy.on_extra_tool(agent, name, args, ctx)), ctx.commands()

    def is_done(self, ctx):
        return bool(self.policy.is_done(ctx))


@dataclass
class _Pending:
    req: ToolRequest
    decision: Decision


@dataclass
class EpisodeResult:
    rounds: int
    turns: int
    ended_by: str  # is_done | next_speaker_none | max_rounds
    submitted: bool
    executed: int
    denied: int
    deferred: int
    policy_errors: int
    spoke: dict[str, int] = field(default_factory=dict)


class TurnHandle:
    def __init__(self, engine: "Engine", agent: str):
        self.engine, self.agent = engine, agent
        self.tool_calls = 0

    def say(self, text: str) -> None:
        self.engine._say(self.agent, text)

    def tool(self, name: str, args: dict | None = None, rationale: str | None = None) -> str:
        if self.tool_calls >= self.engine.max_tool_calls_per_turn:
            return "error: tool call limit for this turn reached"
        self.tool_calls += 1
        return self.engine._tool(self.agent, name, dict(args or {}), rationale)


class Engine:
    def __init__(
        self,
        agents: list[str],
        policy: PolicyHost,
        base_tools: list[ToolSpec],
        executor: Executor,
        *,
        log: EventLog | None = None,
        phase: int | None = None,
        max_rounds: int = 15,
        max_tool_calls_per_turn: int = 8,
        submit_tool: str | None = "submit_final_report",
        models: dict[str, str] | None = None,
    ):
        self.agents = list(agents)
        self.policy = policy
        self.base_tools = list(base_tools)
        self.base_names = {t.name for t in base_tools}
        self.executor = executor
        self.log = log
        self.phase = phase
        self.max_rounds = max_rounds
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.submit_tool = submit_tool
        self.models = models or {}
        self.history: list[Message] = []
        self.pending: dict[str, _Pending] = {}
        self.turn_count = {a: 0 for a in self.agents}
        self.spoke = {a: 0 for a in self.agents}
        self.last_speaker: str | None = None
        self.submitted = False
        self.round = 0
        self.counts = {"execute": 0, "deny": 0, "defer": 0}
        self._ids = itertools.count(1)
        self.extra = policy.extra_tools()
        self.extra = [t for t in self.extra if t.name not in self.base_names]
        self.extra_names = {t.name for t in self.extra}
        if hasattr(policy, "round"):
            policy.round = 0

    # ---- helpers ----------------------------------------------------------------

    def _emit(self, type: str, payload: dict, agent: str | None = None) -> None:
        if self.log is not None:
            self.log.emit(type, payload, phase=self.phase, round=self.round, agent=agent,
                          model=self.models.get(agent) if agent else None)

    def ctx(self) -> Context:
        return Context(
            round=self.round, phase=self.phase or 0, agents=list(self.agents),
            history=[Message(**vars(m)) for m in self.history],
            pending=[p.req for p in self.pending.values()],
            turn_count=dict(self.turn_count), last_speaker=self.last_speaker,
            max_rounds=self.max_rounds, submitted=self.submitted,
        )

    def tools(self) -> list[ToolSpec]:
        return self.base_tools + self.extra

    @staticmethod
    def _resolve(visible_to, agent: str) -> list[str] | str:
        if visible_to == "all":
            return "all"
        if visible_to == "self" or not isinstance(visible_to, list):
            return [agent]
        return sorted(set(visible_to) | {agent})

    def _post(self, msg: Message) -> None:
        self.history.append(msg)

    def _apply(self, commands: list[dict]) -> None:
        for c in commands:
            rid = c.get("request_id")
            p = self.pending.pop(rid, None)
            if p is None:
                self._emit("policy_error", {"hook": "command", "kind": "unknown_request", "detail": c})
                continue
            if c.get("op") == "release":
                self._emit("policy_decision", {"request_id": rid, "action": "release",
                                               "tool": p.req.tool}, p.req.agent)
                self._execute(p.req, p.decision.visible_to)
            elif c.get("op") == "reject":
                reason = c.get("reason", "")
                self._emit("policy_decision", {"request_id": rid, "action": "reject", "reason": reason,
                                               "tool": p.req.tool}, p.req.agent)
                self.counts["deny"] += 1
                self._post(Message("system", f"Request {rid} ({p.req.tool}) was rejected: {reason}",
                                   self.round, kind="notice", visible_to=[p.req.agent], request_id=rid))
            else:
                self.pending[rid] = p
                self._emit("policy_error", {"hook": "command", "kind": "unknown_op", "detail": c})

    def _execute(self, req: ToolRequest, visible_to) -> str:
        vis = self._resolve(visible_to, req.agent)
        self._emit("tool_execution", {"request_id": req.request_id, "tool": req.tool, "args": req.args,
                                      "visible_to": vis}, req.agent)
        try:
            result = self.executor(req.agent, req.tool, req.args)
        except Exception as e:  # executor bugs must not end the episode
            result = f"error: {type(e).__name__}: {e}"
        self._emit("tool_result", {"request_id": req.request_id, "tool": req.tool, "result": result},
                   req.agent)
        self.counts["execute"] += 1
        self._post(Message(req.agent, f"{req.tool}({req.args})", self.round, kind="tool_call",
                           visible_to=vis, request_id=req.request_id))
        self._post(Message("system", result, self.round, kind="tool_result",
                           visible_to=vis if vis == "all" else list(vis), request_id=req.request_id))
        if req.tool == self.submit_tool:
            self.submitted = True
        return result

    # ---- actions available to actors ------------------------------------------

    def _say(self, agent: str, text: str) -> None:
        if not text:
            return
        self.spoke[agent] += 1
        self._emit("message", {"text": text}, agent)
        self._post(Message(agent, text, self.round, kind="message", visible_to="all"))
        self._apply(self.policy.on_message(agent, text, self.ctx()))

    def _tool(self, agent: str, name: str, args: dict, rationale: str | None) -> str:
        if name in self.extra_names:
            self._emit("tool_request", {"tool": name, "args": args, "extra": True,
                                        "rationale": rationale}, agent)
            result, cmds = self.policy.on_extra_tool(agent, name, args, self.ctx())
            self._emit("tool_result", {"tool": name, "result": result, "extra": True}, agent)
            self._post(Message(agent, f"{name}({args})", self.round, kind="tool_call", visible_to=[agent]))
            self._post(Message("system", result, self.round, kind="tool_result", visible_to=[agent]))
            self._apply(cmds)
            return result
        if name not in self.base_names:
            return f"error: unknown tool {name!r}"
        req = ToolRequest(f"req-{next(self._ids)}", agent, name, args, rationale)
        self._emit("tool_request", {"request_id": req.request_id, "tool": name, "args": args,
                                    "rationale": rationale}, agent)
        decision, cmds = self.policy.on_tool_request(req, self.ctx())
        self._emit("policy_decision", {"request_id": req.request_id, "tool": name,
                                       "action": decision.action, "reason": decision.reason,
                                       "visible_to": decision.visible_to}, agent)
        if decision.action == "execute":
            result = self._execute(req, decision.visible_to)
        elif decision.action == "deny":
            self.counts["deny"] += 1
            result = f"denied by team policy: {decision.reason}".rstrip(": ")
        else:
            self.counts["defer"] += 1
            self.pending[req.request_id] = _Pending(req, decision)
            result = f"deferred ({req.request_id}): {decision.reason}".rstrip(": ")
        self._apply(cmds)
        return result

    # ---- main loop ----------------------------------------------------------------

    def run(self, actor: Callable[..., None]) -> EpisodeResult:
        turns, ended_by = 0, "max_rounds"
        n = len(self.agents)
        for self.round in range(1, self.max_rounds + 1):
            if hasattr(self.policy, "round"):
                self.policy.round = self.round
            stop = False
            for _ in range(n):
                speaker, cmds = self.policy.next_speaker(self.ctx())
                self._apply(cmds)
                if speaker is None:
                    ended_by, stop = "next_speaker_none", True
                    break
                if speaker == SKIP:
                    continue
                visible, cmds = self.policy.visible_context(speaker, self.ctx())
                self._apply(cmds)
                self.turn_count[speaker] += 1
                self.last_speaker = speaker
                turns += 1
                actor(speaker, visible, self.tools(), TurnHandle(self, speaker))
                if self.policy.is_done(self.ctx()):
                    ended_by, stop = "is_done", True
                    break
            if stop:
                break
        return EpisodeResult(
            rounds=self.round, turns=turns, ended_by=ended_by, submitted=self.submitted,
            executed=self.counts["execute"], denied=self.counts["deny"], deferred=self.counts["defer"],
            policy_errors=getattr(self.policy, "error_count", 0), spoke=dict(self.spoke),
        )
