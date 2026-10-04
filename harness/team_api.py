"""Types and context helpers exposed to team-harness code.

Team code may import only this module plus an allowlisted stdlib subset. It
returns decisions; it never executes tools. Everything here is plain data so
it can be serialized across the restricted-subprocess boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Visibility = list[str] | Literal["all", "self"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON schema


@dataclass
class Message:
    sender: str  # agent name, or "system"
    text: str
    round: int = 0
    kind: str = "message"  # message | tool_call | tool_result | notice
    visible_to: list[str] | str = "all"  # "all" or explicit agent names (outer resolves "self")
    request_id: str | None = None  # set for tool_call / tool_result entries

    def visible_for(self, agent: str) -> bool:
        if self.visible_to == "all" or self.sender == agent:
            return True
        return isinstance(self.visible_to, list) and agent in self.visible_to


@dataclass
class ToolRequest:
    request_id: str
    agent: str
    tool: str
    args: dict[str, Any]
    rationale: str | None = None


@dataclass
class Decision:
    action: Literal["execute", "deny", "defer"]
    reason: str = ""
    visible_to: Visibility = "self"

    def __post_init__(self) -> None:
        if self.action not in ("execute", "deny", "defer"):
            raise ValueError(f"invalid action: {self.action!r}")
        if isinstance(self.visible_to, str) and self.visible_to not in ("all", "self"):
            raise ValueError(f"invalid visible_to: {self.visible_to!r}")


class StateStore:
    """Small JSON-serializable key-value store, persisted between hook calls
    within one episode and wiped between episodes."""

    def __init__(self, data: dict[str, Any] | None = None):
        self._data: dict[str, Any] = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass
class Context:
    """Read-only view of the episode passed to every hook, plus a queue of
    release/reject commands the outer harness applies after the hook returns."""

    round: int
    phase: int
    agents: list[str]
    history: list[Message]
    pending: list[ToolRequest]
    turn_count: dict[str, int] = field(default_factory=dict)
    last_speaker: str | None = None
    max_rounds: int = 0
    submitted: bool = False
    _commands: list[dict[str, str]] = field(default_factory=list)

    def release(self, request_id: str) -> None:
        self._commands.append({"op": "release", "request_id": request_id})

    def reject(self, request_id: str, reason: str = "") -> None:
        self._commands.append({"op": "reject", "request_id": request_id, "reason": reason})

    def commands(self) -> list[dict[str, str]]:
        return list(self._commands)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_commands")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Context":
        d = dict(d)
        d["history"] = [Message(**m) for m in d.get("history", [])]
        d["pending"] = [ToolRequest(**r) for r in d.get("pending", [])]
        return cls(**d)
