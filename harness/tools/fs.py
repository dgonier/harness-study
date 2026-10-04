"""Sandboxed filesystem tools: list_dir, read_file, write_file.

Paths are resolved relative to a workspace root. Reads are allowed under
`read_roots`, writes under `write_roots` (both relative to the root). Any
path that escapes the allowed roots, including via `..` or symlinks, is
refused. Tools return plain strings for the model; write_file also returns
an edit record for the outer harness to log.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 100_000
MAX_WRITE_BYTES = 500_000
MAX_LIST_ENTRIES = 500


class SandboxError(Exception):
    pass


@dataclass
class EditRecord:
    path: str
    existed: bool
    before: str | None
    after: str
    diff: str


class Sandbox:
    def __init__(self, root: str | os.PathLike, read_roots=(".",), write_roots=(".",),
                 hidden=()):
        self.root = Path(root).resolve()
        self.read_roots = [self._abs_root(r) for r in read_roots]
        self.write_roots = [self._abs_root(r) for r in write_roots]
        self.hidden = [self._abs_root(h) for h in hidden]  # never listed/read/written

    def _abs_root(self, rel: str) -> Path:
        return (self.root / rel).resolve()

    @staticmethod
    def _under(path: Path, base: Path) -> bool:
        return path == base or base in path.parents

    def resolve(self, rel: str, *, write: bool = False) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise SandboxError("path must be a non-empty string")
        if "\x00" in rel:
            raise SandboxError("invalid path")
        p = (self.root / rel.lstrip("/")).resolve()
        if any(self._under(p, h) for h in self.hidden):
            raise SandboxError(f"path not found: {rel}")
        roots = self.write_roots if write else self.read_roots
        if not any(self._under(p, r) for r in roots):
            kind = "write to" if write else "access"
            raise SandboxError(f"cannot {kind} {rel!r}: outside the allowed directories")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    # ---- tools ----------------------------------------------------------------

    def list_dir(self, path: str = ".") -> str:
        p = self.resolve(path)
        if not p.exists():
            raise SandboxError(f"path not found: {path}")
        if not p.is_dir():
            raise SandboxError(f"not a directory: {path}")
        entries = []
        for child in sorted(p.iterdir()):
            r = child.resolve()
            if any(self._under(r, h) for h in self.hidden) or child.name == "__pycache__":
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) > MAX_LIST_ENTRIES:
            entries = entries[:MAX_LIST_ENTRIES] + [f"... ({len(entries) - MAX_LIST_ENTRIES} more)"]
        return "\n".join(entries) if entries else "(empty)"

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise SandboxError(f"file not found: {path}")
        data = p.read_bytes()
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_READ_BYTES:
            text += f"\n... [truncated, {len(data)} bytes total]"
        return text

    def write_file(self, path: str, content: str) -> tuple[str, EditRecord]:
        if not isinstance(content, str):
            raise SandboxError("content must be a string")
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise SandboxError(f"content too large (max {MAX_WRITE_BYTES} bytes)")
        p = self.resolve(path, write=True)
        if p.is_dir():
            raise SandboxError(f"is a directory: {path}")
        existed = p.is_file()
        before = p.read_text(encoding="utf-8", errors="replace") if existed else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        rel = self.rel(p)
        diff = "".join(difflib.unified_diff(
            (before or "").splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"a/{rel}" if existed else "/dev/null", tofile=f"b/{rel}"))
        verb = "updated" if existed else "created"
        return f"{verb} {rel} ({len(content)} chars)", EditRecord(rel, existed, before, content, diff)


def diff_against_template(template_root: str | os.PathLike, workspace_root: str | os.PathLike,
                          rel: str) -> str:
    """Unified diff of one workspace file against its template version."""
    t = Path(template_root) / rel
    w = Path(workspace_root) / rel
    a = t.read_text(encoding="utf-8", errors="replace") if t.is_file() else None
    b = w.read_text(encoding="utf-8", errors="replace") if w.is_file() else None
    return "".join(difflib.unified_diff(
        (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
        fromfile=f"template/{rel}" if a is not None else "/dev/null",
        tofile=f"workspace/{rel}" if b is not None else "/dev/null"))
