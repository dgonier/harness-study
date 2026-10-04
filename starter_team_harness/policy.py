"""Starter team policy: the simplest thing that works.

- Turns go round-robin: Agent-1, Agent-2, Agent-3, Agent-1, ...
- Any agent can call any tool; every request is executed immediately.
- Tool calls and their results are private: only the caller sees them.
- Everyone sees every channel message.
- The phase ends when a final report has been submitted.

Edit this file (or add files next to it) to change how your team coordinates.
See README.md for the hook API.
"""
from team_api import Decision


class TeamPolicy:
    def __init__(self, agents, tools, state):
        self.agents = agents
        self.tools = tools
        self.state = state

    def next_speaker(self, ctx):
        turn = self.state.get("turn", 0)
        self.state.set("turn", turn + 1)
        return self.agents[turn % len(self.agents)]

    def visible_context(self, agent, ctx):
        return [m for m in ctx.history if m.visible_for(agent)]

    def on_tool_request(self, req, ctx):
        return Decision("execute", visible_to="self")

    def on_message(self, agent, text, ctx):
        pass

    def extra_tools(self):
        return []

    def on_extra_tool(self, agent, name, args, ctx):
        return f"unknown tool {name!r}"

    def is_done(self, ctx):
        return ctx.submitted
