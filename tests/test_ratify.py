from harness.ratify import Ratification, diff_trees, snapshot, version_hash

AGENTS = ["Agent-1", "Agent-2", "Agent-3"]


def make_tree(tmp_path):
    d = tmp_path / "team_harness"
    (d / "state").mkdir(parents=True)
    (d / "policy.py").write_text("x = 1\n")
    (d / "README.md").write_text("hello\n")
    return d


def test_hash_changes_on_edit_but_not_state(tmp_path):
    d = make_tree(tmp_path)
    h0 = version_hash(d)
    (d / "state" / "votes.json").write_text("{}")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "policy.cpython-311.pyc").write_bytes(b"\0")
    assert version_hash(d) == h0
    (d / "policy.py").write_text("x = 2\n")
    assert version_hash(d) != h0


def test_hash_sensitive_to_file_names(tmp_path):
    d = make_tree(tmp_path)
    h0 = version_hash(d)
    (d / "README.md").rename(d / "NOTES.md")
    assert version_hash(d) != h0


def test_edit_after_ratification_voids_it(tmp_path):
    d = make_tree(tmp_path)
    r = Ratification(AGENTS)
    h1 = version_hash(d)
    r.note_version(h1)
    for a in AGENTS[:2]:
        assert r.ratify(a, h1, h1)[0]
    (d / "policy.py").write_text("x = 3\n")
    h2 = version_hash(d)
    r.note_version(h2)
    assert r.ratified_by(h2) == []
    ok, msg = r.ratify("Agent-3", h1, h2)  # stale hash refused
    assert not ok and h2 in msg
    for a in AGENTS:
        r.ratify(a, h2, h2)
    assert r.is_unanimous(h2)
    assert r.rejected_versions(h2) == [h1]


def test_unknown_agent_refused():
    r = Ratification(AGENTS)
    assert not r.ratify("Agent-9", "h", "h")[0]


def test_diff_trees(tmp_path):
    d = make_tree(tmp_path)
    before = snapshot(d)
    (d / "policy.py").write_text("x = 1\ny = 2\n")
    (d / "roles.py").write_text("AUDITOR = 'Agent-2'\n")
    out = diff_trees(before, snapshot(d))
    assert "+y = 2" in out and "b/roles.py" in out and "README" not in out
