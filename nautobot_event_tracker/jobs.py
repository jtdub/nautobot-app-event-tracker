"""Jobs for nautobot_event_tracker.

One Job, and it holds nothing but a Job's own concerns: the input, the logging and the JobResult.
The loop it launches lives in `services/agent.py`, which is where every rule about what an agent
may do is written and checked.

Why a Job at all, rather than a Celery task or a view that blocks: ADR 0009. A Job gives an agent
run the three things it needs and none of the things it must not have - a permission to run it, a
time limit, and a place a person can read what happened - while the approval gate stays a database
row that no runtime can wave through.
"""

from nautobot.apps.jobs import Job, JobHookReceiver, ObjectVar, register_jobs

from nautobot_event_tracker.choices import AgentRunStatusChoices, TicketStatusChoices
from nautobot_event_tracker.models import EventTicket
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services import rag as rag_service

name = "Event Tracker"  # pylint: disable=invalid-name


class EventTicketAgentJob(Job):
    """Investigate one event ticket with the agent, stopping at anything that changes the network."""

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        name = "Investigate an Event Ticket"
        description = (
            "Runs the agent over one ticket: it reads the ticket, calls the read-only MCP tools an "
            "operator has enabled, and writes what it found back onto the ticket. A tool that "
            "changes the network is proposed rather than called, and the run ends there until "
            "somebody approves or denies it."
        )
        # A ticket ID is not a credential. Saying so is what lets an operator schedule this Job or
        # put a Nautobot Approval Workflow in front of it; the default of True blocks both.
        has_sensitive_variables = False
        # Above the soft limit, or Nautobot kills the Job without the Job ever finding out - which
        # is exactly the silent half-finished run rule A6 exists to prevent.
        soft_time_limit = 600
        # From the service, so the number the Job is killed at and the number the service uses to
        # decide a run's process must be dead cannot drift apart.
        time_limit = agent_service.JOB_TIME_LIMIT_SECONDS

    ticket = ObjectVar(
        model=EventTicket,
        description="The ticket to investigate. One run per ticket at a time.",
    )

    def run(self, ticket):  # pylint: disable=arguments-differ
        """Run or resume the agent, and report what it did in the terms a person needs.

        Every refusal the service makes is a message rather than a traceback: agents being switched
        off, a model that is not callable and a ticket that already has a run are all things an
        operator fixes by doing something, and each of them says what.
        """
        run = agent_service.run_agent(ticket=ticket, user=self.user, job_result=self.job_result)

        self.logger.info(
            "Agent run %s finished as '%s' after %d model call(s).",
            run.pk,
            run.status,
            run.iterations,
            extra={"object": ticket},
        )

        if run.status == AgentRunStatusChoices.WAITING_APPROVAL:
            proposal = agent_service.pending_call(run)
            self.logger.warning(
                "The agent proposed calling '%s' and is waiting for a decision. Nothing has been called.",
                proposal.tool if proposal is not None else "a tool",
                extra={"object": ticket},
            )
        elif run.status == AgentRunStatusChoices.FAILED:
            # Raised rather than logged, so the JobResult is red. A run that failed and reported
            # itself as a success is the failure mode rule A6 is named for.
            raise RuntimeError(run.error or "The agent run failed.")

        return f"Agent run {run.pk}: {run.status}"


class IndexClosedTicket(JobHookReceiver):
    """Index a ticket into the retrieval corpus when it closes (Phase 5A, rule R4).

    A Job Hook rather than a call from `services/tickets.py`, for three reasons in order of
    weight. It catches a close made by *any* path - the UI, the REST transition action,
    `walk_to_status` from a management command, an agent that is one day allowed to close. It keeps
    the ticket service free of any knowledge that retrieval exists, which is the dependency
    direction every other phase has kept. And it runs outside the closing transaction, which a
    network call has to.

    To use it, create a Job Hook in Nautobot on Event Ticket, for updates, pointing at this
    receiver. `docs/admin/rag.md` says so with the clicks.
    """

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta attributes."""

        name = "Index a Closed Event Ticket"
        description = (
            "Embeds a ticket when it reaches closed, so later tickets can be matched against it. "
            "Does nothing for a ticket in any other state, and never prevents a close."
        )
        has_sensitive_variables = False

    def receive_job_hook(self, change, action, changed_object):
        """Index the ticket, if this change is the one that closed it.

        Fires on every change to every ticket, so the first thing it does is decide this is not its
        business. `index_ticket_quietly` is R5: whatever happens next, the close has already
        happened and nothing here may undo it.
        """
        if not isinstance(changed_object, EventTicket):
            return
        if changed_object.status != TicketStatusChoices.CLOSED:
            return

        embedding = rag_service.index_ticket_quietly(changed_object)
        if embedding is None:
            self.logger.info(
                "Nothing indexed for this ticket - retrieval is off, or the attempt failed and was "
                "logged. The close is unaffected.",
                extra={"object": changed_object},
            )
            return
        self.logger.info(
            "Indexed into the retrieval corpus (%d dimensions).",
            embedding.dimensions,
            extra={"object": changed_object},
        )


#: Nautobot imports `<app>.jobs.jobs` at startup and expects to find the Job classes here, which is
#: also what makes `register_jobs()` below run at all: without this name the module is never
#: imported and the Job never appears in the Jobs list.
jobs = [EventTicketAgentJob, IndexClosedTicket]

register_jobs(*jobs)
