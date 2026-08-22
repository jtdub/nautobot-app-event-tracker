"""Jobs for nautobot_event_tracker.

One Job, and it holds nothing but a Job's own concerns: the input, the logging and the JobResult.
The loop it launches lives in `services/agent.py`, which is where every rule about what an agent
may do is written and checked.

Why a Job at all, rather than a Celery task or a view that blocks: ADR 0009. A Job gives an agent
run the three things it needs and none of the things it must not have - a permission to run it, a
time limit, and a place a person can read what happened - while the approval gate stays a database
row that no runtime can wave through.
"""

from nautobot.apps.jobs import Job, ObjectVar, register_jobs

from nautobot_event_tracker.choices import AgentRunStatusChoices
from nautobot_event_tracker.models import EventTicket
from nautobot_event_tracker.services import agent as agent_service

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
        time_limit = 660

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


#: Nautobot imports `<app>.jobs.jobs` at startup and expects to find the Job classes here, which is
#: also what makes `register_jobs()` below run at all: without this name the module is never
#: imported and the Job never appears in the Jobs list.
jobs = [EventTicketAgentJob]

register_jobs(*jobs)
