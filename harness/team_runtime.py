"""Hosts a team policy in a restricted subprocess and enforces per-hook timeouts.

The outer harness talks to the team policy only through TeamRuntime. Every hook
call returns a usable value: if the team code raises, times out, returns
something malformed, or trips a sandbox guard, the runtime logs a
`policy_error` event and substitutes a safe default (deny the tool, skip the
turn, show only the agent's own messages, ...). A timed-out worker is killed
and respawned from the last good state.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .eventlog import EventLog
from .team_api import Context, Decision, Message, ToolRequest, ToolSpec

HARNESS_ROOT = str(Path(__file__).resolve().parent.parent)
WORKER = str(Path(__file__).resolve().parent / "_team_worker.py")
SKIP = "__skip__"  # next_speaker fallback: skip this turn
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024


def _limit_resources() -> None:  # runs in the child before exec
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


class TeamRuntime:
    def __init__(
        self,
        team_dir: str | os.PathLike,
        state_dir: str | os.PathLike,
        agents: list[str],
        tools: list[ToolSpec],
        *,
        timeout_s: float = 2.0,
        log: EventLog | None = None,
        phase: int | None = None,
    ):
        self.team_dir = str(Path(team_dir).resolve())
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.agents = list(agents)
        self.tools = list(tools)
        self.timeout_s = timeout_s
        self.log = log
        self.phase = phase
        self.round: int | None = None
        self.state: dict[str, Any] = {}
        self.proc: subprocess.Popen | None = None
        self.healthy = False  # False if __init__ itself failed
        self.error_count = 0
        self._spawn()

    # ---- process management -------------------------------------------------

    def _spawn(self) -> None:
        self.close()
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"}
        self.proc = subprocess.Popen(
            [sys.executable, "-I", WORKER, HARNESS_ROOT, self.team_dir, str(self.state_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(self.state_dir), env=env, text=True, bufsize=1,
            preexec_fn=_limit_resources if os.name == "posix" else None,
        )
        reply = self._raw_call("__init__", {"agents": self.agents, "tools": [asdict(t) for t in self.tools]})
        self.healthy = reply is not None and reply.get("ok", False)

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f and f.close()
                except Exception:
                    pass
            self.proc = None

    def __enter__(self) -> "TeamRuntime":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low-level call -------------------------------------------------------

    def _error(self, hook: str, kind: str, detail: str, **extra: Any) -> None:
        self.error_count += 1
        if self.log is not None:
            self.log.emit("policy_error", {"hook": hook, "kind": kind, "detail": detail, **extra},
                          phase=self.phase, round=self.round)

    def _raw_call(self, hook: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Send one request. Returns the reply dict, or None on timeout/crash
        (already logged; the worker is respawned for the next call)."""
        assert self.proc is not None
        msg = json.dumps({"hook": hook, "args": args, "state": self.state}, default=str)
        try:
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
            ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout_s)
            line = self.proc.stdout.readline() if ready else None
        except (BrokenPipeError, OSError, ValueError) as e:
            line, crash = None, str(e)
        else:
            crash = None
        if not line:
            kind = "timeout" if crash is None and self.proc.poll() is None else "crash"
            self._error(hook, kind, crash or f"no reply within {self.timeout_s}s")
            if hook != "__init__":
                self._spawn()  # restart from last good state
            return None
        reply = json.loads(line)
        for v in reply.get("violations", []):
            self._error(hook, v["kind"], v["detail"])
        if not reply.get("ok"):
            self._error(hook, "exception", reply.get("error") or "", traceback=reply.get("traceback"))
        else:
            if reply.get("state") is not None:
                self.state = reply["state"]
        return reply

    def _call(self, hook: str, args: dict[str, Any]) -> tuple[bool, Any, list[dict]]:
        if not self.healthy:
            return False, None, []
        reply = self._raw_call(hook, args)
        if reply is None or not reply.get("ok"):
            return False, None, []
        return True, reply.get("result"), reply.get("commands", [])

    # ---- hook API (always returns a usable value) ----------------------------

    def next_speaker(self, ctx: Context) -> tuple[str | None, list[dict]]:
        ok, r, cmds = self._call("next_speaker", {"ctx": ctx.to_dict()})
        if not ok:
            return SKIP, []
        if r is not None and r not in self.agents:
            self._error("next_speaker", "bad_return", f"unknown agent {r!r}")
            return SKIP, cmds
        return r, cmds

    def visible_context(self, agent: str, ctx: Context) -> tuple[list[Message], list[dict]]:
        fallback = [m for m in ctx.history if m.sender in (agent, "system")]
        ok, r, cmds = self._call("visible_context", {"agent": agent, "ctx": ctx.to_dict()})
        if not ok:
            return fallback, []
        try:
            return [Message(**{k: v for k, v in m.items() if k != "__type__"}) for m in r], cmds
        except Exception as e:
            self._error("visible_context", "bad_return", repr(e))
            return fallback, cmds

    def on_tool_request(self, req: ToolRequest, ctx: Context) -> tuple[Decision, list[dict]]:
        ok, r, cmds = self._call("on_tool_request", {"req": asdict(req), "ctx": ctx.to_dict()})
        if not ok:
            return Decision("deny", reason="team policy error"), []
        try:
            return Decision(r["action"], r.get("reason", ""), r.get("visible_to", "self")), cmds
        except Exception as e:
            self._error("on_tool_request", "bad_return", repr(e))
            return Decision("deny", reason="team policy returned an invalid decision"), cmds

    def on_message(self, agent: str, text: str, ctx: Context) -> list[dict]:
        ok, _, cmds = self._call("on_message", {"agent": agent, "text": text, "ctx": ctx.to_dict()})
        return cmds if ok else []

    def extra_tools(self) -> list[ToolSpec]:
        ok, r, _ = self._call("extra_tools", {})
        if not ok or not r:
            return []
        try:
            return [ToolSpec(**{k: v for k, v in t.items() if k != "__type__"}) for t in r]
        except Exception as e:
            self._error("extra_tools", "bad_return", repr(e))
            return []

    def on_extra_tool(self, agent: str, name: str, args: dict, ctx: Context) -> tuple[str, list[dict]]:
        ok, r, cmds = self._call("on_extra_tool", {"agent": agent, "name": name, "args": args,
                                                   "ctx": ctx.to_dict()})
        if not ok:
            return f"error: tool {name!r} failed in the team harness", []
        return str(r), cmds

    def is_done(self, ctx: Context) -> bool:
        ok, r, _ = self._call("is_done", {"ctx": ctx.to_dict()})
        return bool(r) if ok else False
