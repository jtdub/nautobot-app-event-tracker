# Agent Tool Call

One tool call an agent asked for, and what became of it. Written by the service layer as part of an
[Agent Run](agentrun.md); never created from a form.

## The difference between the two kinds of call, in one column

| Status | How a call gets here |
| --- | --- |
| Proposed | The agent asked for it. A read-only call is executed straight away; a mutating one stops here and waits for a person. |
| Approved | Somebody approved it. The next run makes it. |
| Denied | Somebody denied it. The run ends. |
| Executed | It ran and the server answered. |
| Failed | It ran and did not come back usable, or it was refused. |

A read-only call goes to `executed` or `failed` and never has a decider. That is the entire
difference between a tool that reads and a tool that changes something, and it is the boolean on
the [MCP Tool](mcptool.md) — nothing else.

## Fields

| Field | Description |
| --- | --- |
| Run | The run that asked for it. |
| Tool | The registry entry. Protected: the record of a call outlives a tidy-up of the registry. |
| Arguments | What the model asked for, as it asked. Frozen at proposal. |
| Tool Fingerprint | The tool's definition digest when this was proposed. |
| Decided By / Decided At | Who approved or denied it, and when. |
| Result | What came back, capped. |
| Error | Why it failed, when it did. |
| Latency | How long the call took. |
| Proposed At / Called At | When the model asked, and when the call reached the server. |

## Approving and denying

Both need the **Approve Agent Tool Call** permission, which `change` does not imply: approving a
call against the network is not the same right as editing a row. Both are POST-only routes, from
the buttons on the ticket page or on this record's own page, or from
`POST /api/plugins/event-tracker/agent-tool-calls/{id}/approve/`.

Three rules a decision obeys:

- **A decision needs a person.** Not by policy — the trail entry is a `human` write, and the ticket
  service refuses one of those without a user. An AI cannot approve its own proposal because the
  function it would have to call will not let it.
- **A call is decided once.** The trail already says what happened.
- **The arguments are frozen.** Approving approves what was proposed, byte for byte. If you want
  different arguments, deny, say why in a ticket comment, and run the agent again.

Denying ends the run. The agent does not get to answer a denial with something slightly different,
which is how an approver ends up negotiating with a model.

## What is re-checked at execution time

Approving is not a token that stays valid. When the resumption run makes the call, `services/mcp.py`
checks, before any network traffic:

- the tool is still enabled, on a server that is still enabled;
- the call is still `approved`, and has not already run;
- the tool's **definition fingerprint** still matches the one stored on this call.

The last of these covers a narrow but real case. Discovery can find a tool's description or schema
changed, which disables it; an operator can review the new definition and enable it again; and the
proposal an approver is still looking at was written against the old one. What was approved was a
call on the tool *as it read then*.

## Elsewhere

`/api/plugins/event-tracker/agent-tool-calls/` and GraphQL, read-only apart from the two decision
actions.
