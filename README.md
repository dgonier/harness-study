# harness-study

Infrastructure for a multi-agent experiment: a team of LLM agents gets a
task that cannot be completed honestly, and we vary **who designs the team's
coordination mechanism**: the experimenter, or the agents themselves (in a
Phase 1 design step, before they know the task is impossible). The full
build spec is the project brief; this README tracks what exists and what is
left.

## Architecture

- **Outer harness** (`harness/`): ours. Owns model calls, tool execution,
  the ground-truth event log, phase control, ratification and contract
  tests.
- **Team harness**: a `TeamPolicy` class that only *returns decisions*
  through a fixed hook API (`harness/team_api.py`). It runs in a restricted
  subprocess (`harness/team_runtime.py`) with an import allowlist, a
  `state/`-only `open()`, an audit hook that blocks filesystem, process and
  network access, and per-hook timeouts with safe fallbacks.

## Status

| Component | File(s) | Status |
|---|---|---|
| Ground-truth event log | `harness/eventlog.py` | done |
| Team API types | `harness/team_api.py` | done |
| Restricted team runtime | `harness/team_runtime.py`, `harness/_team_worker.py` | done |
| Config loader | `harness/config.py` | done |
| Version hashing + ratification | `harness/ratify.py` | done |
| Starter team harness | `starter_team_harness/` | done |
| `imposed_vote` policy | `harness/policies/imposed_vote/` | done |
| Sandboxed file tools | `harness/tools/fs.py` | done |
| Turn engine | `harness/episode.py` | done |
| Phase 1 meta-protocol | `harness/meta_protocol.py` | done |
| Contract tests | `harness/contract_tests.py` | done |
| Phase 1 orchestration + dry run | `harness/phase1.py`, `scenario/dryrun_template/` | done |
| Model loop (litellm + scripted fake) | `harness/agent.py` | **TODO**: notes only |
| `run_tests` tool | `harness/tools/tests.py` | **TODO**: notes only |
| Analysis scripts | `analysis/` | **TODO**: see `analysis/NOTES.md` |

Run the tests with `pip install -r requirements.txt && python -m pytest -q`.

## TODO

Pieces left for the experimenter (several were interrupted mid-write and
left as notes in place):

1. **`harness/agent.py`**: per-agent model loop. litellm backend for real
   models plus a scripted fake backend for tests; plugs into the engine as an
   *actor* `actor(agent, visible, tools, turn)` using `turn.say()` /
   `turn.tool()`. Design notes are in the file.
2. **`harness/tools/tests.py`**: the `run_tests` tool (pytest over the
   workspace, honoring `eval/config.yaml`, sandboxed subprocess). Notes in
   the file.
3. **Phase 2 scenario** (`scenario/phase2_template/`): `src/`, the 20 tests
   (5 mutually contradictory), `eval/config.yaml`, `logs/debug.log`,
   `README.md`, as described in the spec.
4. **Phase 2 tools**: `http_get` + `harness/mock_server.py`,
   `toggle_logging`, `submit_final_report` (`harness/tools/net.py`,
   `harness/tools/ops.py`), and their `ToolSpec`s.
5. **`docs/phase2_tools.md`**: the Phase 2 tool list shown to agents in
   Phase 1 (currently a placeholder). It must match the `ToolSpec`s passed
   to `run_phase1(phase2_tools=...)`.
6. **Prompts**: `prompts/system.md` and `prompts/phase2_task.md`
   (`prompts/phase1_brief.md` exists).
7. **`harness/outer.py` + `run.py`**: episode setup and reset, condition
   wiring (`solo`, `unilateral`, `imposed_vote`, `self_designed`, `yoked`;
   for `yoked`, load the source harness byte-identically) and the CLI.
   Most of the machinery exists: `Engine`, `TeamRuntime`, `run_phase1`.
8. **`harness/detectors.py`**: Phase 2 cheat and honesty detectors.
9. **Analysis**: `analysis/render.py`, `analysis/harness_features.py`,
   `analysis/summarize.py` (see `analysis/NOTES.md`).
10. **Remaining spec tests**: 1 (scripted episodes for every condition),
    4 (honeypot), 5 (non-mock hosts fail), 7 (yoked byte-identical), 8
    (detectors), 9 (workspace reset). Tests 2, 3 and 6 are covered in
    `tests/`.
11. **`configs/`**: example YAML configs per condition.
12. **Pilot**: 5 episodes per condition with mixed teams, then
    `RESULTS_pilot.md`. Needs model API keys.

## Known limitations

- The team-code sandbox is Python-level plus rlimits. It catches casual and
  accidental misuse and logs every attempt, but it is not a hard boundary
  against deliberately adversarial code. Use OS-level isolation (seccomp,
  containers) if that matters.
- Contract tests drive the harness with scripted cooperative agents. These
  agents approve deferred requests only through team-defined tools that take
  a `request_id` argument, so an unusual approval scheme can fail the
  contract tests even if it would work with real agents.
