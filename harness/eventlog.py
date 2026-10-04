"""Append-only JSONL ground-truth event log.

One file per episode, written by the outer harness only. The log directory
must live outside every agent-accessible path; the outer harness enforces that
when it constructs the episode layout.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

EVENT_TYPES = frozenset({
    "message",
    "tool_request",
    "policy_decision",
    "tool_execution",
    "tool_result",
    "reasoning",
    "file_edit",
    "ratify",
    "phase_start",
    "phase_end",
    "policy_error",
    "model_input",
    "model_output",
    "model_error",
})


class EventLog:
    def __init__(self, path: str | os.PathLike, episode: str):
        self.path = Path(path)
        self.episode = episode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        # Opened in append mode so an existing log is never truncated.
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        phase: int | None = None,
        round: int | None = None,
        agent: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type!r}")
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "episode": self.episode,
                "phase": phase,
                "round": round,
                "agent": agent,
                "model": model,
                "type": type,
                "payload": payload or {},
            }
            self._fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        return event

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_events(path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
