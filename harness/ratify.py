"""Team-harness version hashing and unanimous ratification.

A version hash covers every file under team_harness/ except state/ and
caches, so any edit produces a new hash. Ratifications are recorded against a
specific hash; they count only while that hash is still the current version,
so an edit after ratification voids earlier approvals.
"""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {"state", "__pycache__", ".pytest_cache"}


def iter_files(root: str | Path) -> list[Path]:
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts[:-1]) & EXCLUDED_DIRS) and not p.name.endswith(".pyc"):
            out.append(p)
    return out


def version_hash(root: str | Path) -> str:
    root = Path(root)
    h = hashlib.sha256()
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix().encode()
        data = p.read_bytes()
        h.update(len(rel).to_bytes(4, "big") + rel + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()[:16]


def snapshot(root: str | Path) -> dict[str, str]:
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in iter_files(root)}


def diff_trees(old: dict[str, str], new: dict[str, str]) -> str:
    chunks = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        chunks.extend(difflib.unified_diff(
            (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
            fromfile=f"a/{name}" if a is not None else "/dev/null",
            tofile=f"b/{name}" if b is not None else "/dev/null",
        ))
    return "".join(chunks)


@dataclass
class Ratification:
    agents: list[str]
    approvals: dict[str, str] = field(default_factory=dict)  # agent -> hash they approved
    versions_seen: list[str] = field(default_factory=list)

    def note_version(self, h: str) -> None:
        if not self.versions_seen or self.versions_seen[-1] != h:
            self.versions_seen.append(h)

    def ratify(self, agent: str, h: str, current: str) -> tuple[bool, str]:
        if agent not in self.agents:
            return False, f"unknown agent {agent!r}"
        if h != current:
            return False, f"version {h} is not the current version ({current}); re-read and ratify {current}"
        self.approvals[agent] = h
        return True, f"{agent} ratified {h} ({len(self.ratified_by(current))}/{len(self.agents)})"

    def ratified_by(self, current: str) -> list[str]:
        return [a for a in self.agents if self.approvals.get(a) == current]

    def is_unanimous(self, current: str) -> bool:
        return len(self.ratified_by(current)) == len(self.agents)

    def rejected_versions(self, final: str | None) -> list[str]:
        """Versions that existed during the phase but were not the final ratified one."""
        return [v for v in self.versions_seen if v != final]
