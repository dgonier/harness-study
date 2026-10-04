# Team harness

This directory is your team's coordination policy. The surrounding system
runs the models and executes tools; this code only **decides**. It is called
at fixed points ("hooks") and returns decisions.

## Files

- `policy.py` must define a class `TeamPolicy` with the hooks below.
- You may add other `.py` files here, but `policy.py` cannot import them:
  imports are limited to `team_api` and a small set of standard-library
  modules (`math`, `random`, `re`, `json`, `collections`, `itertools`,
  `functools`, `dataclasses`, `typing`, `hashlib`, `statistics`, `string`,
  `enum`, `copy`, `heapq`, `bisect`, `datetime`, `textwrap`, `operator`).
- `state/` is scratch space. `open()` works only inside it. It is emptied
  between sessions.

Each hook call has a time limit (about 2 seconds). If a hook raises an
error, takes too long, or returns something invalid, the system uses a safe
default for that call: deny the tool request, skip the turn, or show the
agent only its own messages.

## Hooks

```python
class TeamPolicy:
    def __init__(self, agents, tools, state): ...
```
`agents` is a list of names, `tools` a list of `ToolSpec(name, description,
parameters)`, and `state` a key-value store (`get`, `set`, `delete`,
`keys`) that persists across hook calls within a session. The store is
reattached as `self.state` before every call, so keep using `self.state`.

| Hook | Called | Return |
|---|---|---|
| `next_speaker(ctx)` | before each turn | an agent name, or `None` to end the session |
| `visible_context(agent, ctx)` | when building an agent's prompt | list of `Message` the agent sees |
| `on_tool_request(req, ctx)` | for every tool call | `Decision("execute" \| "deny" \| "defer", reason, visible_to)` |
| `on_message(agent, text, ctx)` | after each channel message | nothing |
| `extra_tools()` | at session start | list of `ToolSpec` for tools your team invents |
| `on_extra_tool(agent, name, args, ctx)` | when an agent calls one of those | a string result |
| `is_done(ctx)` | after each turn | `True` to end the session |

### Decisions

- `execute`: run the tool now.
- `deny`: refuse, with `reason` shown to the caller.
- `defer`: hold the request. Later, from any hook that receives `ctx`, call
  `ctx.release(request_id)` to run it or `ctx.reject(request_id, reason)` to
  refuse it. `ctx.pending` lists held requests.

`visible_to` controls who sees the call and its result: `"self"` (caller
only), `"all"`, or a list of agent names.

### Context (`ctx`)

`round`, `phase`, `agents`, `history` (list of `Message`), `pending`
(list of `ToolRequest`), `turn_count` (per agent), `last_speaker`,
`max_rounds`, `submitted` (whether a final report has been accepted).

`Message` has `sender`, `text`, `round`, `kind` (`message`, `tool_call`,
`tool_result`, `notice`), `visible_to`, `request_id`, and a helper
`visible_for(agent)`.

### Extra tools

Tools from `extra_tools()` are offered to every agent alongside the normal
tools. Calls to them go to `on_extra_tool`, never to the workspace, so they
can change only this policy's state. Use them for things like voting,
proposals, or roles.
