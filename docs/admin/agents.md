# Running the Agent

The agent works one ticket. It reads the ticket, calls the read-only MCP tools you have enabled,
writes what it found back onto the ticket, and stops dead at anything that would change the
network.

It is off until you turn it on, and it is started by a person — never by an event.

## What you need first

1. **A provider and a model**, registered and enabled. See
   [Configuring LLM Providers](llm.md).
2. **The `mcp` extra and at least one enabled tool**, if you want the agent to be able to look at
   anything. See [Registering MCP Servers](mcp.md). An agent with no enabled tools still works —
   it reads the ticket and comments — it just cannot go and check.

## Switching it on

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "agent": {
            "enabled": True,
            "provider": "Production OpenAI",   # LLMProvider name
            "model": "gpt-4o-mini",            # LLMModel name on it
        },
    },
}
```

Everything else has a default:

| Key | Default | What it bounds |
| --- | --- | --- |
| `max_iterations` | 8 | Model calls in one run. |
| `max_tool_calls` | 20 | Tool calls across a whole chain, resumptions included. |
| `max_tool_result_chars` | 8000 | How much of one tool's answer is kept. |
| `max_context_chars` | 8000 | How much of the ticket's raw payload goes into the prompt. |
| `max_transcript_chars` | 60000 | How large a run's transcript may grow. |
| `timeout_seconds` | 60 | One model call. |
| `tool_timeout_seconds` | 30 | One tool call. |

Reaching any of the four size bounds ends the run cleanly, with what it found and a line on the
ticket saying which bound it hit. There is no spend cap: a run is bounded individually, and a
deployment that launches many runs can still spend a great deal.

A value that cannot work — a bound of zero, agents enabled with no model named — is refused at the
first run with a message naming every fault at once.

## Running it

**Investigate with Agent**, on a ticket's page. It leads to the Job's own run form with the ticket
filled in, so you see the Job, its description and its time limit before anything starts.

The same Job is in **Jobs → Event Tracker → Investigate an Event Ticket**, and it can be scheduled
there like any other. Running it needs Nautobot's `run` permission on that Job.

!!! note "Enable the Job first"
    Nautobot registers a newly installed Job **disabled**, as it does for every app. Tick
    **Enabled** on it once, in the Jobs list, or the button leads to a Job that will not start.

One run per ticket at a time. Launching again while one is running, or while one is waiting on a
decision, is refused with a message pointing at the run that holds it.

## What the agent may do to a ticket

| It may | It may not |
| --- | --- |
| Comment | Resolve or close |
| Move a `new` ticket to `triaged` | Assign, or change severity |
| Propose a mutating tool call | Make one |

Everything it does is recorded as `ai` with no user. The person who launched the run is on the run,
not on the ticket's trail — they started an investigation, they did not make its findings.

An agent cannot touch a resolved or closed ticket at all. That is rule S3, the same rule that
governs triage, and it is checked before a model is called.

## The approval gate

When the agent asks for a tool marked **Mutating**, three things happen and then the run ends:

1. An **Agent Tool Call** is written in `proposed`, carrying the exact arguments.
2. A `tool_proposed` entry appears on the ticket's trail, naming the server, the tool and those
   arguments in full.
3. The run finishes as **Waiting for Approval** and gives its worker back.

Nothing has been called. The proposal can sit over a weekend at no cost.

**Approve** or **Deny** on the ticket page. Approving records the decision and enqueues a
resumption run, which makes the call and carries on from where the first run stopped. Denying ends
the chain.

Both need the **Approve Agent Tool Call** permission, which `change` does not imply. See
[Agent Tool Call](../models/agenttoolcall.md) for what is re-checked when the approved call
actually runs.

## What this does not protect you from

Worth stating plainly, because the controls above are about blast radius rather than about
reasoning.

A ticket's payload is written by whoever can put a line on a topic you consume. An agent reading it
can be steered.

- An attacker **cannot** make the agent call a tool nobody enabled, or run a mutating tool without
  a person pressing Approve.
- An attacker **can** try to steer which read-only tools get called and with what arguments, and to
  influence the comment the agent leaves. A read-only tool is not a harmless tool if it reads
  something sensitive.
- An attacker **can** try to make a mutating proposal look reasonable to a tired approver at 03:00.
  The gate is only as good as the person reading it, which is why the proposal shows the server,
  the tool and the exact arguments rather than the model's summary of them.

A deployment that cannot accept the third of these enables no mutating tools at all. That is a
supported configuration, and the agent is still useful in it.

## Where to look afterwards

- The ticket's **Update Trail** — proposed, decided, executed, and the agent's conclusion.
- The ticket's **Agent Runs** panel, and each run's **Transcript**: exactly what the model was told
  and exactly what it asked for.
- The ticket's **LLM Usage** panel — every model call the agent made, priced, alongside triage's.

## Further reading

- [Agent Run](../models/agentrun.md) and [Agent Tool Call](../models/agenttoolcall.md)
- [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md) — why a run ends at the gate
- [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) — transport and default-deny
