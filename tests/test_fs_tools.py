import os

import pytest

from harness.tools.fs import Sandbox, SandboxError, diff_against_template


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "team_harness").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "secret").mkdir()
    (root / "team_harness" / "policy.py").write_text("a = 1\n")
    (root / "docs" / "api.md").write_text("# API\n")
    (root / "secret" / "x.txt").write_text("nope")
    return root


def test_scoped_read_write(ws):
    sb = Sandbox(ws, read_roots=("team_harness", "docs"), write_roots=("team_harness",))
    assert sb.read_file("docs/api.md") == "# API\n"
    msg, rec = sb.write_file("team_harness/policy.py", "a = 2\n")
    assert msg.startswith("updated") and rec.existed and "+a = 2" in rec.diff
    _, rec = sb.write_file("team_harness/sub/new.py", "b = 1\n")
    assert not rec.existed and (ws / "team_harness" / "sub" / "new.py").exists()
    with pytest.raises(SandboxError):
        sb.write_file("docs/api.md", "x")
    with pytest.raises(SandboxError):
        sb.read_file("secret/x.txt")


@pytest.mark.parametrize("path", ["../outside.txt", "team_harness/../../outside.txt", "/etc/passwd",
                                  "team_harness/\x00x", ""])
def test_escapes_refused(ws, path):
    sb = Sandbox(ws, read_roots=("team_harness",), write_roots=("team_harness",))
    with pytest.raises(SandboxError):
        sb.write_file(path, "x")
    assert not (ws.parent / "outside.txt").exists()


def test_symlink_escape_refused(ws, tmp_path):
    (tmp_path / "target.txt").write_text("outside")
    os.symlink(tmp_path / "target.txt", ws / "team_harness" / "link.txt")
    sb = Sandbox(ws, read_roots=("team_harness",), write_roots=("team_harness",))
    with pytest.raises(SandboxError):
        sb.read_file("team_harness/link.txt")
    with pytest.raises(SandboxError):
        sb.write_file("team_harness/link.txt", "x")
    assert (tmp_path / "target.txt").read_text() == "outside"


def test_hidden_paths(ws):
    sb = Sandbox(ws, hidden=("secret",))
    assert "secret" not in sb.list_dir(".")
    with pytest.raises(SandboxError, match="not found"):
        sb.read_file("secret/x.txt")


def test_list_and_errors(ws):
    sb = Sandbox(ws)
    assert sb.list_dir("team_harness") == "policy.py"
    with pytest.raises(SandboxError):
        sb.list_dir("team_harness/policy.py")
    with pytest.raises(SandboxError):
        sb.read_file("team_harness/missing.py")


def test_diff_against_template(ws, tmp_path):
    tmpl = tmp_path / "tmpl" / "team_harness"
    tmpl.mkdir(parents=True)
    (tmpl / "policy.py").write_text("a = 1\n")
    (ws / "team_harness" / "policy.py").write_text("a = 1\nb = 2\n")
    d = diff_against_template(tmp_path / "tmpl", ws, "team_harness/policy.py")
    assert "+b = 2" in d


def test_list_root_shows_only_allowed_dirs(ws):
    sb = Sandbox(ws, read_roots=("team_harness", "docs"), write_roots=("team_harness",))
    assert sb.list_dir(".") == "docs/\nteam_harness/"
    with pytest.raises(SandboxError):
        sb.list_dir("..")
    with pytest.raises(SandboxError):
        sb.list_dir("secret")
