import pytest

from harness.config import ConfigError, from_dict

BASE = {
    "experiment": "t", "condition": "unilateral",
    "agents": [{"name": "Agent-1", "model": "fake"}, {"name": "Agent-2", "model": "fake"}],
}


def test_defaults_and_nested():
    cfg = from_dict({**BASE, "phase2": {"max_rounds": 5}})
    assert cfg.phase2.max_rounds == 5 and cfg.phase2.hook_timeout_s == 2.0
    assert cfg.phase1.max_rounds == 25 and not cfg.has_phase1


def test_solo_uses_first_agent():
    cfg = from_dict({**BASE, "condition": "solo"})
    assert [a.name for a in cfg.active_agents] == ["Agent-1"]


@pytest.mark.parametrize("patch", [
    {"condition": "anarchy"},
    {"agents": []},
    {"agents": [{"name": "A", "model": "m"}, {"name": "A", "model": "m"}]},
    {"agents": [{"name": "A", "model": "m"}]},
    {"condition": "yoked"},
    {"bogus_key": 1},
    {"phase2": {"max_roundz": 3}},
])
def test_invalid(patch):
    with pytest.raises(ConfigError):
        from_dict({**BASE, **patch})
