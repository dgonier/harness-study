"""run_tests tool. NOT YET IMPLEMENTED -- placeholder notes only.

Writing this module was interrupted; to be completed later.

Intended behavior (from the build spec):
- Run pytest over whatever is currently on disk in the episode workspace.
- Honor the workspace's grader config (`eval/config.yaml`: `pass_threshold`,
  `skip`) when computing the reported pass rate.
- Run workspace code in a subprocess with a minimal env, a timeout and
  rlimits; `unshare -rn` (no network namespace) works in this container.
- Write the machine-readable report (e.g. JUnit XML) outside the workspace,
  parse per-test outcomes, return a short text summary to the model plus a
  structured result for the event log and detectors.
"""
