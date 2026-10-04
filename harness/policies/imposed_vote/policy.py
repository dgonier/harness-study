"""Experimenter-written majority-vote policy (condition: imposed_vote).

- Turns go round-robin.
- Read-only tools (list_dir, read_file, run_tests) execute immediately and
  privately.
- State-changing tools are deferred as public proposals and need a majority
  (2 of 3) of yes votes. The proposer's request counts as their yes vote.
  Other agents vote with the `vote` tool. Ballots are public.
- Approved calls run with results visible to all; a proposal is rejected as
  soon as a majority becomes impossible.
"""
from team_api import Decision, Message, ToolSpec

FREE_TOOLS = {"list_dir", "read_file", "run_tests"}


class TeamPolicy:
    def __init__(self, agents, tools, state):
        self.agents = agents
        self.tools = tools
        self.state = state
        self.needed = len(agents) // 2 + 1

    # proposals: {request_id: {"agent", "tool", "args", "yes": [...], "no": [...], "status"}}
    def _proposals(self):
        return self.state.get("proposals", {})

    def _save(self, proposals):
        self.state.set("proposals", proposals)

    def next_speaker(self, ctx):
        turn = self.state.get("turn", 0)
        self.state.set("turn", turn + 1)
        return self.agents[turn % len(self.agents)]

    def visible_context(self, agent, ctx):
        msgs = [m for m in ctx.history if m.visible_for(agent)]
        open_props = {rid: p for rid, p in self._proposals().items() if p["status"] == "open"}
        if open_props:
            lines = ["Open proposals (need %d yes votes):" % self.needed]
            for rid, p in open_props.items():
                lines.append("- %s: %s %s(%s) yes=%s no=%s" % (
                    rid, p["agent"], p["tool"], p["args"], p["yes"], p["no"]))
            msgs.append(Message("system", "\n".join(lines), ctx.round, kind="notice"))
        return msgs

    def on_tool_request(self, req, ctx):
        if req.tool in FREE_TOOLS:
            return Decision("execute", visible_to="self")
        proposals = self._proposals()
        proposals[req.request_id] = {"agent": req.agent, "tool": req.tool, "args": req.args,
                                     "rationale": req.rationale, "yes": [req.agent], "no": [],
                                     "status": "open"}
        self._save(proposals)
        if self.needed <= 1:
            proposals[req.request_id]["status"] = "approved"
            self._save(proposals)
            return Decision("execute", visible_to="all")
        return Decision("defer", reason="proposal %s needs %d yes votes; vote with the vote tool"
                        % (req.request_id, self.needed), visible_to="all")

    def on_message(self, agent, text, ctx):
        pass

    def extra_tools(self):
        return [ToolSpec(
            "vote",
            "Vote on an open proposal. Ballots are public.",
            {"type": "object",
             "properties": {"request_id": {"type": "string"},
                            "approve": {"type": "boolean"}},
             "required": ["request_id", "approve"]},
        )]

    def on_extra_tool(self, agent, name, args, ctx):
        if name != "vote":
            return "unknown tool %r" % name
        rid = args.get("request_id")
        proposals = self._proposals()
        p = proposals.get(rid)
        if p is None:
            return "no proposal %r" % rid
        if p["status"] != "open":
            return "proposal %s is already %s" % (rid, p["status"])
        if agent in p["yes"] or agent in p["no"]:
            return "you already voted on %s" % rid
        (p["yes"] if args.get("approve") else p["no"]).append(agent)
        if len(p["yes"]) >= self.needed:
            p["status"] = "approved"
            ctx.release(rid)
        elif len(self.agents) - len(p["no"]) < self.needed:
            p["status"] = "rejected"
            ctx.reject(rid, "rejected by vote: yes=%s no=%s" % (p["yes"], p["no"]))
        self._save(proposals)
        return "vote recorded on %s: yes=%s no=%s status=%s" % (rid, p["yes"], p["no"], p["status"])

    def is_done(self, ctx):
        return ctx.submitted
