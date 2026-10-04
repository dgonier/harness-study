# Analysis scripts: not yet implemented

Writing these was interrupted; to be completed later. See the build spec for
full requirements. Inputs per episode (already produced by the harness):

- `ground_truth/events.jsonl`: the append-only event log (`harness/eventlog.py`)
- `ground_truth/versions.jsonl`, `ground_truth/versions/<hash>/`, `<hash>.diff`
  (Phase 1 version history; see `harness/phase1.py`)
- `ratified_harness/`: the frozen harness used in Phase 2

Planned files:

- `render.py`: Markdown transcript that interleaves channel messages, each
  agent's tool requests (marked private/shared), policy decisions, results,
  policy errors, ratifications, and Phase 1 harness diffs. Reasoning shown
  separately and marked as never visible to other agents.
- `harness_features.py`: rule-based features of each ratified harness (which
  tools are gated and how, visibility defaults, voting/proposals/roles/halt/
  own audit log/sanctions/amendment mechanism, authorship share per agent from
  `file_edit` events, rounds to ratification, fallback, rejected versions).
- `summarize.py`: per-episode CSV. Coordination metrics can come straight from
  the event log; the cheat/honesty metrics depend on `harness/detectors.py`.
