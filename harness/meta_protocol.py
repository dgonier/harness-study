"""Phase 1 bootstrap protocol (ours, trusted, runs in-process).

Agents cannot use a governance system to decide on the governance system, so
Phase 1 always runs under this fixed protocol: round-robin turns, one shared
channel, every tool call and result (including every edit) visible to all.
The phase ends when the current harness version is unanimously ratified; the
outer harness enforces `phase1_max_rounds` as the cap.
"""
from __future__ import annotations

from typing import Callable

from .team_api import Decision


class MetaProtocol:
    def __init__(self, agents: list[str], is_ratified: Callable[[], bool]):
        self.agents = list(agents)
        self.is_ratified = is_ratified
        self.turn = 0

    def next_speaker(self, ctx):
        speaker = self.agents[self.turn % len(self.agents)]
        self.turn += 1
        return speaker

    def visible_context(self, agent, ctx):
        return list(ctx.history)

    def on_tool_request(self, req, ctx):
        return Decision("execute", visible_to="all")

    def on_message(self, agent, text, ctx):
        pass

    def extra_tools(self):
        return []

    def on_extra_tool(self, agent, name, args, ctx):
        return f"unknown tool {name!r}"

    def is_done(self, ctx):
        return self.is_ratified()
