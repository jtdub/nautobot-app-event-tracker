# Agent Run

One pass of the agent over one ticket. A record of what happened, not a thing anybody edits: no
form creates one, and no API route writes one.

A run is started by the **Investigate an Event Ticket** Job, from the button on a ticket's page or
from the Jobs list. See [Running the Agent](../admin/agents.md).

## Where a run got to

| Status | What it means |
| --- | --- |
| Running | Executing now. |
| Waiting for Approval | The agent asked for a tool that changes something. Nothing has been called; somebody has to decide. |
| Completed | It finished, and its conclusion is a comment on the ticket. |
| Denied | Somebody denied its proposal. The chain ends there. |
| Failed | Something went wrong, and the ticket says what. |
| Superseded | Its approved call was taken over by a resumption run, which is the one that made it. |

**Waiting for Approval is a finished run.** It is not blocked, it is not holding a worker, and it
is not waiting on a lock. The loop reaches the approval gate by *ending*, and approving starts a
new run from the stored transcript. That is [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md),
and it is why a proposal can sit for three days over a weekend without costing anything.

## Fields

| Field | Description |
| --- | --- |
| Ticket | The one ticket this run is about. Deleting it deletes its runs. |
| Started By | The person who launched it. **Not** the actor: everything the model decided is recorded on the ticket as `ai` with no user. |
| Job Result | The Nautobot-side record of the same run, with its log. |
| Parent | The run this one resumed, when it is a resumption. |
| Transcript | Every message, tool call and result, in the order the model saw them. |
| Iterations | Model calls spent. |
| Error | Why the run failed, when it did. |
| Started At / Finished At | |

## The transcript

The whole prompt, stored. It is worth reading for two different reasons.

The mundane one: resumption replays it, so an approved call continues the same conversation rather
than starting a new one.

The other one: when an agent proposes something surprising, the transcript is where you find out
why. Every control in this app assumes the ticket's payload is written by whoever can put a line on
a consumed topic, and that an agent reading it can be steered. What the payload cannot do is call a
tool nobody enabled, or run a mutating one without a person pressing Approve. What it *can* do is
influence what the agent concludes and which read-only tools it reaches for — and the transcript is
where that shows.

## What bounds a run

Four limits, all configured in the `agent` block, and reaching any of them ends the run
`completed` with what it has and a line on the ticket saying which limit it was:

| Limit | Bounds |
| --- | --- |
| `max_iterations` | Model calls in one run. |
| `max_tool_calls` | Tool calls across a whole chain, resumptions included. |
| `max_tool_result_chars` | How much of one tool's answer is kept. |
| `max_transcript_chars` | How large the transcript may grow. |

`max_tool_calls` counts across the chain deliberately. Counted per run, it would mean "twenty calls
per approval", which is not a limit.

## Elsewhere

`/api/plugins/event-tracker/agent-runs/` and GraphQL, both read-only.
