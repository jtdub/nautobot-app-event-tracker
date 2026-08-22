# 0009 — Agent runs are Nautobot Jobs that end at the approval gate

**Status:** Accepted
**Phase:** 4B

## Context

[ADR 0007](0007-mcp-tools-streamable-http-and-default-deny.md) settled what an agent may reach and named the approval gate: the agent proposes a mutating tool call, a human approves it, and only then does it execute. It did not say what runs the agent, and the two questions turn out to be one question.

An agent run is a bounded unit of work with a beginning and an end — several model calls, some tool calls, a conclusion written to the ticket. That is the shape [ADR 0005](0005-standalone-consumer-process.md) said a Job has and a consumer does not, and the argument runs the other way here: a Job is exactly right for this.

The gate is what makes it hard. A run that reaches a mutating proposal has to wait for a person, and people take hours. Three things are true at once:

- A Celery task that blocks holds a worker slot for as long as it blocks. This is the objection ADR 0005 raised against running the consumer as a task, and it does not become acceptable because the thing being waited for is a human rather than a broker.
- Nautobot's own Approval Workflows approve *starting* a Job. They cannot approve a decision the Job makes halfway through its own run, because by then the Job is running.
- A Job with `has_sensitive_variables` left at its default cannot enter an approval workflow at all, since approval needs the variables stored.

## Decision

**An agent run is a Nautobot Job**, `EventTicketAgentJob`, taking one ticket. It is launched by a person — from the Jobs list, from a Job Button on the ticket, or through the REST API — and its `JobResult` is the deployment-visible record that it ran.

**The run ends at the first mutating proposal.** It does not sleep, poll or block. The proposal becomes an `AgentToolCall` row in `proposed`, the run finishes in `waiting_approval`, and the worker slot is released. Approving the call starts a *new* Job run that continues from the stored transcript. A run therefore always terminates, and a ticket waiting three days for an approval costs nothing while it waits.

**The gate is the app's own, not Nautobot's Approval Workflows.** Approval Workflows govern whether a Job may start; this gate governs a decision inside a run that has already started, and it has to record the proposal, the decision, the approver and the result on the ticket's own append-only trail (ADR 0007, ADR 0001). An operator may still put an Approval Workflow in front of the Job itself; the two compose and answer different questions.

**Read-only tools execute inside the run.** Only mutating tools end it. A run that never proposes a mutation completes in one Job.

## Consequences

**Good.** No worker slot is ever held waiting for a person. Every run terminates, which makes `time_limit` meaningful and makes a stuck run impossible rather than merely unlikely. The transcript has to be durable for resumption to work, and a durable transcript is also the audit record somebody wants when asking what the agent was told.

**Good.** Approval is a row with a status, so it is reachable from the UI, the REST API and a permission — and it is on the ticket, where the rest of the ticket's history is.

**Bad.** A multi-step change is a run per step. An agent that wants to shut an interface, check a session and shut another one costs three approvals and three Job runs, and the operator feels every one of them. This is the intended trade and it is still friction.

**Bad.** The transcript is stored and re-sent on resumption, so a long run pays for its own history in tokens each time it resumes. Rule A2's caps exist to bound that.

**Bad.** Two records now describe one run: the `JobResult` Nautobot keeps and the `AgentRun` the app keeps. They can disagree if a worker is killed between them, and the app has to treat the `AgentRun` as the one that matters.

## Alternatives considered

**Block inside the Job until approved.** Rejected: it holds a Celery worker for the length of a human's afternoon, which is the failure ADR 0005 exists to avoid.

**Nautobot Approval Workflows on the agent Job.** Rejected as the gate: it approves the run, not the call, so the thing approved would be "may this agent do whatever it decides" — which is precisely the blanket permission ADR 0007 refused. Kept as an optional extra layer in front of the Job.

**A standalone agent process, like the consumer.** Rejected: agent runs are episodic and operator-initiated, so there is nothing to keep running between them, and it would add a second deployment unit to solve a problem Jobs already solve.

**Propose every tool call, read-only ones included.** Rejected: a run that stops for approval before it may read an interface's state cannot investigate anything, and the gate would be worn down by approvals that carry no risk — which is how gates stop being read.
