# Agent Run

An Agent Run is one pass of the agent over one ticket. It is a record of what happened. Nobody edits it. The web interface has no add form and no edit form. The REST endpoint is read-only.

The **Investigate an Event Ticket** Job starts a run. Click **Investigate with Agent** on the ticket page, or start the Job in **Jobs > Jobs**. See [Running the Agent](../admin/agents.md).

## Run statuses {#where-a-run-got-to}

| Status | What it means |
| --- | --- |
| **Running** | The Job executes the run now. |
| **Waiting for Approval** | The agent proposed a call of a writable tool. The app made no call. A person must decide. |
| **Completed** | The run finished. Its conclusion is a comment on the ticket. |
| **Denied** | A person denied the proposal. The chain ends. |
| **Failed** | The run did not finish. The ticket comment and the **Error** field say why. |
| **Superseded** | A resumption run continued this run after an approval. The resumption run makes the approved call. If the resumption run stops before it starts the call, the call becomes **Failed**. |

**A run with the status Waiting for Approval is a finished run.** It does not block. It does not hold a worker. It does not wait for a lock. The loop reaches the approval gate when it ends. After an approval, a resumption run continues from the stored transcript.

[ADR 0009](../decisions/0009-agent-runs-are-jobs-that-end-at-the-gate.md) gives the reason for this design. A proposal can wait over a weekend at no cost.

## Fields

The detail page of a run shows the bold labels below. The list shows **Started** for `started_at`. It shows **Finished at**, with a small "a", for `finished_at`. The list also has a **Tool Calls** column with the number of calls of the run.

| Field | Description |
| --- | --- |
| **Started At** (`started_at`) | The time when the run started. |
| **Ticket** (`ticket`) | The ticket that the run is about. If you delete the ticket, the app deletes its runs. |
| **Status** (`status`) | The status of the run now. See the table above. |
| **Started By** (`started_by`) | The person who started the run. This person is not the actor. The app records each decision of the model on the ticket as `ai`, with no user. |
| **Iterations** (`iterations`) | The number of model calls that the run used. |
| **Finished At** (`finished_at`) | The time when the run ended. The field is empty while the status is **Running**. |
| **Job Result** (`job_result`) | The Nautobot record of the same run. It has the log of the Job. |
| **Parent** (`parent`) | The run that this run resumed. The field is empty if the run is not a resumption. |
| **Hard Time Limit (seconds)** (`time_limit_seconds`) | The hard time limit of the Job when the run started, in seconds, for example `660.0`. The app uses it to find a run whose worker died. The field is empty for a run from an earlier release. |
| **Error** (`error`) | Why the run failed, if it did. The app cuts a long text. A stop of the Job gives a text that starts with "The Job reached its soft time limit" or "The process running this agent stopped without recording an outcome". |
| **Transcript** (`transcript`) | Each message, tool call and result, in the order that the model saw them. The detail page shows it in its own panel. |

The detail page also has a **Tool Calls** panel. The **Agent Runs** panel of a ticket page lists the runs of that ticket.

## The transcript

The transcript is the whole prompt, stored. It has two uses.

The first use is a resumption. A resumption run replays the transcript. An approved call then continues the same conversation. It does not start a new conversation.

The second use is to see why an agent proposed something unexpected. Each control of the app assumes that any person who can write to a consumed topic can write the payload of a ticket. The controls also assume that the payload can steer the agent.

The payload cannot make the agent call a tool that is not enabled. The payload cannot make the agent call a writable tool without the approval of a person. The payload can change the conclusions of the agent and the read-only tools that it calls. The transcript shows this.

## Bounds of a run {#what-bounds-a-run}

A run has five bounds. Four bounds are settings in the `agent` block. The fifth bound is the run time limit, which comes from the time limits of the Job. See [The run time limit](../admin/agents.md#the-run-time-limit).

If a run reaches one of these bounds, the run is **Completed** with what the agent found. The agent writes a line on the ticket that names the bound. If the Job reaches its soft time limit, the run is **Failed**.

| Setting | Bound |
| --- | --- |
| `max_iterations` | The number of model calls in one run. |
| `max_tool_calls` | The number of tool calls in the whole chain, with the resumption runs. |
| `max_tool_result_chars` | The size of one tool answer that the app keeps. |
| `max_transcript_chars` | The maximum size of the transcript. |

`max_tool_calls` counts the calls of the whole chain, on purpose. A count for each run would allow a new set of calls at each approval. That is not a limit.

## Elsewhere

The REST API serves runs at `/api/plugins/event-tracker/agent-runs/`. GraphQL also serves them. Both are read-only.
