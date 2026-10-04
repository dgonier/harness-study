"""Per-agent model loop. NOT YET IMPLEMENTED -- placeholder notes only.

Intended design (to be written later):

- The outer harness owns every model call; team code never calls models.
- Two backends behind one interface:
    * a litellm backend for real models (per-agent model, temperature,
      api_base, optional api_key_env; retries; drop unsupported params)
    * a scripted fake backend for tests (fixed list of responses per agent,
      or a callable), so every condition can run end to end without API keys
- One call = one agent *turn*: build the prompt (system prompt + the
  visible context the team policy returns), call the model, hand each tool
  call to an `execute(tool_call) -> str` callback supplied by the outer
  harness, feed results back, repeat until no more tool calls or the
  per-turn cap (`max_tool_calls_per_turn`) is reached.
- Tool schemas come from `team_api.ToolSpec`; add an optional `rationale`
  string arg to each tool and strip it into `ToolRequest.rationale`.
- Malformed tool-call JSON: return an error string to the model, log it, do
  not forward to the team policy.
- Logging (via EventLog): `model_input`, `model_output`, `reasoning` (when
  the provider exposes thinking content; never shown to other agents),
  `model_error`. Tool requests/executions are logged by the outer harness.
- A model error ends that agent's turn; it must not crash the episode.
"""
