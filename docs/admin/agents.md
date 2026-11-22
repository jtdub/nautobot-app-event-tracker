# Running the Agent

The agent works on one ticket. It reads the ticket and calls the read-only MCP tools that you enabled. It
writes what it found on the ticket. It stops before any action that changes the network.

The agent is off until you turn it on. A person starts each run. An event never starts a run.

## What you need first

1. **A provider and a model**, registered and enabled. See
   [Configuring LLM Providers](llm.md).
2. **The `mcp` extra and at least one enabled tool**, if the agent must check a system. See
   [Registering MCP Servers](mcp.md). An agent with no enabled tool still works. It reads the ticket and
   writes a comment. It cannot check a system.

## Turn on the agent {#switching-it-on}

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "agent": {
            "enabled": True,
            "provider": "Production OpenAI",   # AI Provider name
            "model": "gpt-4o-mini",            # AI Model name on it
        },
    },
}
```

Each other setting has a default:

| Key | Default | What it bounds |
| --- | --- | --- |
| `max_iterations` | 8 | The number of model calls in one run. |
| `max_tool_calls` | 20 | The number of tool calls in a whole chain, with the resumption runs. |
| `max_tool_result_chars` | 8000 | The size of one tool answer that the app keeps. The app makes separator lines and table spaces short first. |
| `max_context_chars` | 8000 | The size of the raw payload of the ticket that goes into the prompt. |
| `max_transcript_chars` | 60000 | The maximum size of the transcript of a run. |
| `timeout_seconds` | 60 | The time limit of one model call. The agent does not try the call again. |
| `tool_timeout_seconds` | 30 | The time limit of one step of a tool call. A call can take two times this value or more. |

Before the app applies `max_tool_result_chars`, it makes padding short. A run of 10 or more `=`, `-` or `+`
becomes 3 characters. In a table row that starts with `|`, a run of 2 or more spaces becomes one space. The
agent tells the model when the app does this.

A run ends when it reaches one of its five bounds. The run is Completed with what it found. The agent writes
a line on the ticket that names the bound.

One model answer can ask for many tool calls. Each call counts against `max_tool_calls`. A run can reach
`max_tool_calls` before it reaches `max_iterations`. The agent checks the transcript limit before each model
call. The results of one answer can take the transcript past `max_transcript_chars`.

The app has no cost limit. Each run has its own bounds. A deployment that starts many runs can spend a large
amount.

The first run refuses a value that cannot work. Two examples are a bound of zero and an enabled agent with no
model name. The refusal message names each fault.

### The run time limit

A run stops before the time limit of the Job. The run time limit is the lowest of three values. These are the
soft time limit of the Job, its hard time limit, and 600 seconds.

The agent starts a model call only if `timeout_seconds` plus 30 seconds fit before the run time limit. It
starts a read-only tool call only if two times `tool_timeout_seconds` plus 30 seconds fit. With the defaults,
no call starts after 510 seconds. A run that stops this way is Completed. The ticket comment starts with "The
agent stopped at the run time limit".

You can lower the limits of the Job. A higher limit does not give a run more time.

!!! warning
    If a limit is above its default, a run can start on a ticket while an earlier run still has the status **Running**.
    Do not set the soft time limit of this Job above 600 seconds. Do not set the hard time limit above 660
    seconds.

To lower the limits:

1. In **Jobs > Jobs**, open the row menu of **Investigate an Event Ticket**.
2. Click **Edit job**.
3. Next to the limit that you change, select **Override default value**.
4. Change **Soft time limit** or **Time limit**.
5. Click **Update**.

Sometimes `timeout_seconds` plus 30 seconds do not fit in the run time limit. Then each run stops before its
first model call. The Job log has a warning.

If the Job reaches its soft time limit in the middle of a call, the run is Failed. The ticket comment starts
with "The agent run failed: The Job reached its soft time limit". Each tool call that did not finish is Failed.
If the call changes the network, its error says that the change might be in effect. Check the device before
you approve the same change again.

The update trail of the ticket has an entry for each call that the soft time limit cut off. The same thing
happens after a worker dies in the middle of a run. The next run that you start on the ticket marks the
earlier run and its calls Failed.

A resumption run makes an approved call. It does not start if two times `tool_timeout_seconds` plus 30
seconds do not fit in the run time limit. The call stays approved. Set the limits of the Job back to their
defaults, or lower `tool_timeout_seconds`. To restore a default, clear **Override default value** for the
limit. Then run the agent again.

## Run the agent {#running-it}

Click **Investigate with Agent** on the ticket page. The button opens the run form of the Job with the ticket
filled in. Before the run starts, you can read the Job, its description, and its time limit.

You can also start the same Job in **Jobs > Jobs**. It has the name **Investigate an Event Ticket** and
the group **Event Tracker**. You can schedule it there, as you schedule any other Job. To run it, you need the
Nautobot `run` permission on that Job.

!!! note "Enable the Job first"
    Nautobot registers a newly installed Job as **disabled**. This is true for each app. Enable the Job
    once, or the button leads to a Job that does not start.

    1. In **Jobs > Jobs**, open the row menu of the Job.
    2. Click **Edit job**.
    3. Select **Enabled**.
    4. Click **Update**.

A ticket has one run at a time. The Job refuses a new run while an earlier run on the ticket has the status
**Running**. It also refuses a new run while a run waits for a decision. The refusal message gives the reason.

## What the agent may do to a ticket

| It may | It may not |
| --- | --- |
| Comment | Resolve or close |
| Transition a `new` ticket to `triaged` | Assign, or change severity |
| Propose a call of a writable tool | Make the call |

The app records each action of the agent as `ai`, with no user. The run shows the person who started it. The
update trail of the ticket does not show that person. The person started an investigation. The person did not
make its findings.

The agent cannot change a resolved or closed ticket (rule S3). Triage follows the same rule. The app checks
the rule before it makes a model call.

## The approval gate

When a ticket has a proposed call, its page shows the panel **Waiting for Your Decision**. It also shows the
buttons **Approve Tool Call** and **Deny Tool Call**.

![A ticket page with the Waiting for Your Decision panel and the Approve Tool Call and Deny Tool Call buttons](../media/ss_agent_approval_light.png#only-light)
![A ticket page with the Waiting for Your Decision panel and the Approve Tool Call and Deny Tool Call buttons](../media/ss_agent_approval_dark.png#only-dark)

When the agent asks for a tool that has **Writable** selected, the app does three things:

1. It writes an **Agent Tool Call** in `proposed`, with the exact arguments.
2. It adds a `tool_proposed` entry to the update trail of the ticket. The entry names the server, the tool,
   and all the arguments.
3. It ends the run as **Waiting for Approval** and gives the worker back.

At this point, the app does not call the tool. The proposal can wait over a weekend at no cost.

The agent runs the read-only calls of one model answer in order. It stops at the first call of a writable
tool. A call after that call, in the same answer, does not run.

Click **Approve Tool Call** or **Deny Tool Call** on the ticket page. You can also click **Approve** or
**Deny** on the page of the tool call. If you approve, the app records the decision and queues a resumption
run. The resumption run makes the call. It then continues from the place where the first run stopped. If you
deny, the chain ends.

Each button needs the permission "Can approve or deny a proposed agent tool call". The `change` permission
does not include it. See [Agent Tool Call](../models/agenttoolcall.md) for what the app checks again when it
makes the approved call.

## What this does not protect you from

The controls above limit the effect of a mistake. They do not check whether the conclusions of the model are correct.

Anyone who can put a line on a topic that you consume writes the payload of a ticket. That person can steer an
agent that reads the payload.

- An attacker **cannot** make the agent call a tool that nobody enabled. An attacker **cannot** make the agent
  run a writable tool without the approval of a person.
- An attacker **can** try to steer which read-only tools the agent calls, and with which arguments. An
  attacker can also try to influence the comment of the agent. One model answer can cause up to
  `max_tool_calls` read-only calls. A lower `max_tool_calls` lowers that number. A read-only tool is not
  harmless if it reads sensitive data.
- An attacker **can** try to make a writable proposal look reasonable to a tired approver at 03:00. The gate
  depends on the person who reads the proposal. For this reason, the proposal shows the server, the tool, and
  the exact arguments. It does not show a summary of the arguments from the model.

A deployment that cannot accept the third risk enables no writable tool. The app supports this configuration.
The agent is still useful with it.

## Where to look afterwards

An agent run page shows the **Transcript** of the run.

![An agent run page with the Transcript panel, which holds the messages of the run](../media/ss_agent_run_light.png#only-light)
![An agent run page with the Transcript panel, which holds the messages of the run](../media/ss_agent_run_dark.png#only-dark)

- The **Update Trail** of the ticket shows each proposal, each decision, each execution, and the conclusion of
  the agent.
- The **Agent Runs** panel of the ticket lists each run. The **Transcript** of a run shows exactly what the
  model received and exactly what it asked for.
- The **LLM Usage** panel of the ticket shows each model call of the agent, with its price. It also shows the
  model calls of triage.

## Further reading

- [Agent Run](../models/agentrun.md) and [Agent Tool Call](../models/agenttoolcall.md)
- [ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md) — why a run ends at the gate
- [ADR 0007](../decisions/0007-mcp-tools-streamable-http-and-default-deny.md) — transport and default-deny
