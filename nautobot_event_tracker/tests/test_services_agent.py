"""Tests for the agent service: the loop, its bounds, and the gate it stops at.

No test calls a model and no test opens a socket: `complete` and `call_tool` are both seams, and
the fakes route through the real `services.llm` so that every turn leaves a real usage record
behind - which is what makes the accounting assertions here mean anything.
"""

# pylint: disable=too-many-lines
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.utils import timezone
from nautobot_ai_models.choices import MCPTransportChoices

from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMPurposeChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import AgentRun, AgentToolCall, LLMUsageRecord
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services import mcp as mcp_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import (
    AgentBusyError,
    AgentConfigurationError,
    AgentDecisionError,
    LLMCallError,
    LLMConfigurationError,
    MCPConfigurationError,
    MCPDeadlineError,
    TicketImmutableError,
)
from nautobot_event_tracker.tests import fixtures


class AgentTestCase(TestCase):
    """Shared setup: a ticket, a model to call, and the settings that switch agents on."""

    def setUp(self):
        """One ticket and one registered model, which is all a run needs."""
        self.user = fixtures.create_user()
        self.ticket = fixtures.create_ticket(user=self.user, title="leaf-01 interface down")
        self.model = fixtures.create_llmmodel()

    def run_agent(self, *turns, call_tool=None, ticket=None, user=None, **service_kwargs):
        """Run the agent with these scripted model turns, and hand back the run and the fake."""
        complete = fixtures.FakeAgentComplete(*turns)
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=ticket or self.ticket,
                user=user if user is not None else self.user,
                complete=complete,
                call_tool=call_tool if call_tool is not None else fixtures.FakeToolCaller(),
                **service_kwargs,
            )
        return run, complete

    def messages(self, run, role):
        """Every transcript message with this role."""
        return [message for message in run.transcript if message.get("role") == role]

    def updates(self, update_type):
        """Every trail entry of this type on the ticket."""
        return list(self.ticket.updates.filter(update_type=update_type))

    @staticmethod
    def messages_of(messages, role):
        """Every message of a request with this role."""
        return [message for message in messages if message.get("role") == role]


class _EchoingToolCaller(fixtures.FakeToolCaller):  # pylint: disable=too-few-public-methods
    """A fake tool caller whose text names the device of each call."""

    def __call__(self, *, tool_call, timeout=None, max_result_chars=None):
        """Answer with the device of this call."""
        self.text = f"status of {tool_call.arguments['device']}"
        return super().__call__(tool_call=tool_call, timeout=timeout, max_result_chars=max_result_chars)


class _SlowToolCaller(fixtures.FakeToolCaller):  # pylint: disable=too-few-public-methods
    """A fake tool caller that moves a fake clock forward by the time of each call."""

    def __init__(self, clock, seconds, **kwargs):
        """Move this clock by this many seconds in each call."""
        super().__init__(**kwargs)
        self.clock = clock
        self.seconds = seconds

    def __call__(self, **kwargs):
        """Spend the time, then answer."""
        self.clock.advance(self.seconds)
        return super().__call__(**kwargs)


class TestTheSettings(TestCase):
    """The `agent` block: defaults, and the values that cannot work."""

    def test_agents_are_off_by_default(self):
        """An app installed before anybody configured one must not try to call a model.

        Under `app_settings()`, which is a stock install: the app's own `default_settings` and
        nothing else. Read from the ambient configuration instead, this would assert whatever the
        deployment running the tests happens to have set.
        """
        with fixtures.app_settings():
            settings = agent_service.get_settings()

        self.assertFalse(settings.enabled)
        self.assertEqual(settings.max_iterations, 8)

    def test_a_provider_and_model_are_required_when_enabled(self):
        """Switching agents on without naming a model is a fault worth a sentence."""
        with fixtures.app_settings(agent={"enabled": True}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        self.assertIn("'provider' is required", str(caught.exception))
        self.assertIn("'model' is required", str(caught.exception))

    def test_a_bound_must_be_a_positive_integer(self):
        """`True` is an `int` in Python, so the bool clause is the half that matters."""
        for value in (0, -1, True, "eight"):
            with self.subTest(value=value):
                with fixtures.app_settings(agent={"max_iterations": value}):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        agent_service.get_settings()
                self.assertIn("'max_iterations'", str(caught.exception))

    def test_a_timeout_must_be_a_positive_number(self):
        """The same check, for the two keys that are seconds rather than counts."""
        with fixtures.app_settings(agent={"tool_timeout_seconds": 0}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        self.assertIn("'tool_timeout_seconds'", str(caught.exception))

    def test_every_fault_is_reported_at_once(self):
        """One restart answers every fault, rather than the first of them."""
        with fixtures.app_settings(agent={"enabled": True, "max_iterations": 0, "timeout_seconds": -1}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        message = str(caught.exception)
        for fragment in ("'max_iterations'", "'timeout_seconds'", "'provider'", "'model'"):
            self.assertIn(fragment, message)


class TestTheDeadRunCheck(TestCase):
    """AC-1 and AC-2 - a `running` run is dead only after its own hard time limit plus the grace time."""

    START = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)

    def is_dead_after(self, limit, seconds):
        """Return the check for a run that started at `START` with this stored limit, `seconds` later."""
        run = fixtures.create_agentrun(started_at=self.START, time_limit_seconds=limit)
        return agent_service._is_dead(run, self.START + timedelta(seconds=seconds))  # pylint: disable=protected-access

    def test_a_run_with_no_stored_limit_uses_the_default_limit_of_the_job(self):
        """AC-2 - 660 seconds and 300 of grace, so the check of an older release does not move."""
        self.assertFalse(self.is_dead_after(None, 960))
        self.assertTrue(self.is_dead_after(None, 961))

    def test_a_raised_limit_makes_the_run_live_for_longer(self):
        """AC-1 - the fault of issue 43: the check used 960 seconds for a run with a limit of 3060."""
        self.assertFalse(self.is_dead_after(3060, 961))
        self.assertFalse(self.is_dead_after(3060, 3360))
        self.assertTrue(self.is_dead_after(3060, 3361))

    def test_a_lowered_limit_makes_the_run_dead_sooner(self):
        """AC-1 - Celery kills the process at the hard limit, so the limit and the grace time are enough."""
        self.assertFalse(self.is_dead_after(95, 395))
        self.assertTrue(self.is_dead_after(95, 396))

    def test_a_stored_limit_that_is_not_above_zero_is_not_known(self):
        """AC-2 - the service never writes one, and the check does not trust one that is there."""
        for limit in (0, -5):
            with self.subTest(limit=limit):
                self.assertFalse(self.is_dead_after(limit, 960))
                self.assertTrue(self.is_dead_after(limit, 961))

    def test_a_stored_limit_that_a_timedelta_cannot_hold_is_not_known(self):
        """AC-2 - an infinite or huge value is a fault of the configuration, and the ticket must not hold forever."""
        for limit in (float("inf"), 1e300, float("nan")):
            with self.subTest(limit=limit):
                self.assertFalse(self.is_dead_after(limit, 960))
                self.assertTrue(self.is_dead_after(limit, 961))


class TestStartingARun(AgentTestCase):  # pylint: disable=too-many-public-methods
    """What is refused before any run row exists."""

    def test_a_disabled_agent_is_refused(self):
        """The Job is visible to anyone with `run` on it; "not configured" is a sentence."""
        with fixtures.app_settings(), self.assertRaises(AgentConfigurationError) as caught:
            agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("not enabled", str(caught.exception))
        self.assertFalse(AgentRun.objects.exists())

    def test_a_disabled_model_is_refused_before_a_run_exists(self):
        """Rule L8's posture, one layer up: no row exists to say the run could not start."""
        self.model.enabled = False
        self.model.validated_save()

        with fixtures.agent_settings():
            with self.assertRaises(LLMConfigurationError):
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertFalse(AgentRun.objects.exists())

    def test_a_resolved_ticket_is_refused(self):
        """S3 is not relaxed for agents: an AI actor may not touch a finished ticket."""
        ticket = fixtures.create_ticket_in_status(TicketStatusChoices.RESOLVED, user=self.user)

        with fixtures.agent_settings():
            with self.assertRaises(TicketImmutableError):
                agent_service.run_agent(ticket=ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertFalse(AgentRun.objects.exists())

    def test_a_second_run_on_a_running_ticket_is_refused(self):
        """A9 - one live run per ticket, and the message points at the run that holds it."""
        fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.RUNNING)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("still running", str(caught.exception))

    def test_a_run_whose_process_died_does_not_wedge_the_ticket(self):
        """Celery's hard kill takes the worker without the loop seeing anything.

        Nothing marks the row, so it stays `running` forever: every future launch on that ticket
        was refused, and with no edit or delete route the only recovery was a database shell.
        """
        dead = fixtures.create_agentrun(
            ticket=self.ticket,
            status=AgentRunStatusChoices.RUNNING,
            started_at=timezone.now()
            - timedelta(seconds=agent_service.JOB_TIME_LIMIT_SECONDS + agent_service.STALE_RUN_GRACE_SECONDS + 60),
        )

        run, _ = self.run_agent("Looked again.")

        dead.refresh_from_db()
        self.assertEqual(dead.status, AgentRunStatusChoices.FAILED)
        self.assertIn("stopped without recording an outcome", dead.error)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def dead_run(self, **overrides):
        """Return a `running` run whose process has outlived the dead-run check."""
        age = agent_service.JOB_TIME_LIMIT_SECONDS + agent_service.STALE_RUN_GRACE_SECONDS + 60
        return fixtures.create_agentrun(
            ticket=self.ticket,
            status=AgentRunStatusChoices.RUNNING,
            started_at=timezone.now() - timedelta(seconds=age),
            **overrides,
        )

    def test_a_dead_run_s_unfinished_read_only_call_is_failed_and_recorded(self):
        """AC-15 - the next launch marks the call that the dead process left, and the trail says so."""
        dead = self.dead_run()
        tool = fixtures.create_mcptool(name="look", enabled=True, writable=False)
        row = fixtures.create_agenttoolcall(run=dead, tool=tool, status=AgentToolCallStatusChoices.EXECUTING)

        self.run_agent("Looked again.")

        row.refresh_from_db()
        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[0]
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.DEAD_PROCESS_CALL_ERROR)
        self.assertIn("(failed,", entry.message)
        self.assertIn(mcp_service.DEAD_PROCESS_CALL_ERROR, entry.message)

    def test_a_dead_resumption_s_cut_approved_call_is_outcome_unknown_with_the_warning(self):
        """AC-3, AC-7, AC-8 - the call of the parent run gets the warning and its status, and the dead run ends with the note."""
        parent = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.SUPERSEDED)
        tool = fixtures.create_mcptool(name="push_config", enabled=True, writable=True)
        row = fixtures.create_agenttoolcall(run=parent, tool=tool, status=AgentToolCallStatusChoices.EXECUTING)
        dead = self.dead_run(parent=parent)

        self.run_agent("Looked again.")

        row.refresh_from_db()
        dead.refresh_from_db()
        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[0]
        self.assertEqual(row.status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)
        self.assertEqual(row.error, mcp_service.CUT_CHANGE_ERROR)
        self.assertEqual(dead.status, AgentRunStatusChoices.FAILED)
        self.assertTrue(dead.error.endswith(mcp_service.CUT_CHANGE_RUN_NOTE))
        self.assertIn("(outcome_unknown,", entry.message)
        self.assertIn(mcp_service.CUT_CHANGE_ERROR, entry.message)

    def test_a_dead_resumption_s_unstarted_approved_call_is_failed(self):
        """AC-8 - the next launch marks the approved call that the dead run did not start."""
        parent = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.SUPERSEDED)
        tool = fixtures.create_mcptool(name="push_config", enabled=True, writable=True)
        row = fixtures.create_agenttoolcall(run=parent, tool=tool, status=AgentToolCallStatusChoices.APPROVED)
        dead = self.dead_run(parent=parent)

        run, _ = self.run_agent("Looked again.")

        row.refresh_from_db()
        dead.refresh_from_db()
        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[0]
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)
        self.assertIsNone(row.called_at)
        self.assertEqual(dead.status, AgentRunStatusChoices.FAILED)
        self.assertNotIn("might be in effect", dead.error)
        self.assertIn("(failed, 0 ms)", entry.message)
        self.assertIn(mcp_service.UNSTARTED_CALL_ERROR, entry.message)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_a_dead_run_with_no_unfinished_call_keeps_its_error(self):
        """AC-15 - no call, no warning and no trail entry."""
        dead = self.run_agent_dead()

        self.assertNotIn("might be in effect", dead.error)
        self.assertEqual(self.updates(UpdateTypeChoices.TOOL_EXECUTED), [])

    def run_agent_dead(self):
        """Bury a dead run with no calls by launching again, and return the buried run."""
        dead = self.dead_run()
        self.run_agent("Looked again.")
        dead.refresh_from_db()
        return dead

    def test_a_run_still_within_its_time_limit_is_left_alone(self):
        """The sweep must not declare a live run dead: two agents on one ticket is worse."""
        alive = fixtures.create_agentrun(
            ticket=self.ticket,
            status=AgentRunStatusChoices.RUNNING,
            started_at=timezone.now() - timedelta(seconds=30),
        )

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError):
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        alive.refresh_from_db()
        self.assertEqual(alive.status, AgentRunStatusChoices.RUNNING)

    def stale_run(self, ticket, *, age, limit, status=AgentRunStatusChoices.RUNNING, **overrides):
        """Return a run on this ticket that started `age` seconds ago with this stored limit."""
        return fixtures.create_agentrun(
            ticket=ticket,
            status=status,
            started_at=timezone.now() - timedelta(seconds=age),
            time_limit_seconds=limit,
            **overrides,
        )

    def test_a_run_stores_the_hard_time_limit_it_was_given(self):
        """AC-3 - the Job passes its hard limit, and the dead-run check reads it from the row."""
        run, _ = self.run_agent("Done.", time_limit=1200)

        self.assertEqual(run.time_limit_seconds, 1200)

    def test_a_run_stores_no_limit_when_none_is_usable(self):
        """AC-3 - a value that the check could not use is not stored."""
        run, _ = self.run_agent("Done.")
        self.assertIsNone(run.time_limit_seconds)

        for limit in (0, -1, True, "660", float("inf"), 1e300, float("nan")):
            with self.subTest(limit=limit):
                run, _ = self.run_agent("Done.", ticket=fixtures.create_ticket(), time_limit=limit)
                self.assertIsNone(run.time_limit_seconds)

    def test_a_live_run_under_a_raised_limit_is_not_buried(self):
        """AC-1 - 1000 seconds is past the default check, and well inside a limit of 3060."""
        held = self.stale_run(self.ticket, age=1000, limit=3060)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        held.refresh_from_db()
        self.assertIn("still running", str(caught.exception))
        self.assertEqual(held.status, AgentRunStatusChoices.RUNNING)
        self.assertEqual(held.error, "")

    def test_a_dead_run_under_a_raised_limit_is_buried_after_that_limit(self):
        """AC-1 - 3060 seconds and 300 of grace, and then the ticket is free."""
        dead = self.stale_run(self.ticket, age=3420, limit=3060)

        run, _ = self.run_agent("Done.")

        dead.refresh_from_db()
        self.assertEqual(dead.status, AgentRunStatusChoices.FAILED)
        self.assertIn("stopped without recording an outcome", dead.error)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_a_dead_run_under_a_lowered_limit_is_buried_sooner(self):
        """AC-1 - 95 seconds and 300 of grace is 395, so 500 seconds is dead."""
        dead = self.stale_run(self.ticket, age=500, limit=95)

        run, _ = self.run_agent("Done.")

        dead.refresh_from_db()
        self.assertEqual(dead.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_a_run_inside_a_lowered_limit_and_its_grace_time_is_left_alone(self):
        """AC-1 - 300 seconds is under 395."""
        held = self.stale_run(self.ticket, age=300, limit=95)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError):
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        held.refresh_from_db()
        self.assertEqual(held.status, AgentRunStatusChoices.RUNNING)

    def test_an_old_run_that_waits_for_approval_is_not_buried(self):
        """AC-4 - the check applies to `running` only, whatever the age and the stored limit."""
        waiting = self.stale_run(self.ticket, age=5000, limit=95, status=AgentRunStatusChoices.WAITING_APPROVAL)
        fixtures.create_agenttoolcall(run=waiting)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        waiting.refresh_from_db()
        self.assertIn("waiting for a decision", str(caught.exception))
        self.assertEqual(waiting.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual(waiting.error, "")
        self.assertIsNone(waiting.finished_at)

    def test_an_old_finished_run_is_not_touched(self):
        """AC-4 - a finished run is history, however old it is and whatever its stored limit says."""
        finished_at = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        for status in (
            AgentRunStatusChoices.COMPLETED,
            AgentRunStatusChoices.FAILED,
            AgentRunStatusChoices.DENIED,
            AgentRunStatusChoices.SUPERSEDED,
        ):
            with self.subTest(status=status):
                ticket = fixtures.create_ticket()
                old = self.stale_run(ticket, age=5000, limit=95, status=status, error="kept", finished_at=finished_at)

                run, _ = self.run_agent("Done.", ticket=ticket)

                old.refresh_from_db()
                self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
                self.assertEqual(old.status, status)
                self.assertEqual(old.error, "kept")
                self.assertEqual(old.finished_at, finished_at)

    def test_a_run_waiting_on_a_decision_is_refused(self):
        """The gate is a person's turn; launching again does not take it from them."""
        run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        fixtures.create_agenttoolcall(run=run)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("waiting for a decision", str(caught.exception))

    def test_a_finished_run_does_not_block_a_new_one(self):
        """Only live runs hold a ticket. A completed one is history."""
        fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.COMPLETED)

        run, _ = self.run_agent("Looked, found nothing.")

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)


class TestAPlainRun(AgentTestCase):
    """A run that asks for no tools: the simplest thing the loop does."""

    def test_the_model_s_answer_becomes_a_ticket_comment(self):
        """Step 5 - no tool calls means the answer is the conclusion."""
        run, complete = self.run_agent("The interface flapped twice and recovered.")

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(run.iterations, 1)
        self.assertEqual(len(complete.calls), 1)
        comment = self.updates(UpdateTypeChoices.COMMENT)[-1]
        self.assertIn("The interface flapped twice and recovered.", comment.message)

    def test_the_system_prompt_allows_more_than_one_read_only_call_per_turn(self):
        """The prompt states the rule that the code enforces."""
        run, _ = self.run_agent("Nothing to report.")

        self.assertIn("more than one read-only tool call", agent_service.SYSTEM_PROMPT)
        self.assertNotIn("one tool call per turn", agent_service.SYSTEM_PROMPT)
        self.assertEqual(run.transcript[0]["content"], agent_service.SYSTEM_PROMPT)

    def test_the_comment_is_attributed_to_the_ai_and_to_no_user(self):
        """A4 - the person who launched the run is not the actor on what the model decided."""
        self.run_agent("Nothing to report.")

        comment = self.updates(UpdateTypeChoices.COMMENT)[-1]
        self.assertEqual(comment.source, TicketSourceChoices.AI)
        self.assertIsNone(comment.user)

    def test_the_run_records_who_launched_it(self):
        """Not the actor, and still worth knowing."""
        run, _ = self.run_agent("Nothing to report.")

        self.assertEqual(run.started_by, self.user)

    def test_every_model_call_is_accounted(self):
        """A10 - purpose `agent`, linked to the ticket, in the table triage's calls land in."""
        self.run_agent("Nothing to report.")

        record = LLMUsageRecord.objects.get(purpose=LLMPurposeChoices.AGENT)
        self.assertEqual(record.ticket, self.ticket)

    def test_a_new_ticket_is_triaged_by_a_run_that_concluded(self):
        """A5 - the agent moves a ticket forward, by the smallest step there is."""
        self.run_agent("This is a real fault on leaf-01.")

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.TRIAGED)

    def test_a_ticket_already_moved_on_is_left_alone(self):
        """The one transition is for a `new` ticket. Nothing else is the agent's to decide."""
        ticket = fixtures.create_ticket_in_status(TicketStatusChoices.IN_PROGRESS, user=self.user, title="In progress")

        self.run_agent("Still broken.", ticket=ticket)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatusChoices.IN_PROGRESS)

    def test_the_agent_never_ends_a_ticket(self):
        """A5 and 13.1 - closing is the one judgement whose being wrong is invisible."""
        self.run_agent("Fixed. Close this ticket immediately.")

        self.ticket.refresh_from_db()
        self.assertNotIn(self.ticket.status, (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED))

    def test_the_transcript_is_the_record(self):
        """A7 - the instruction, the ticket and the answer, in the order the model saw them."""
        run, _ = self.run_agent("Nothing to report.")

        roles = [message["role"] for message in run.transcript]
        self.assertEqual(roles, ["system", "user", "assistant"])

    def test_the_prompt_carries_the_ticket_and_its_payload(self):
        """Step 3 - and the payload is labelled as data, which is what rule A8 reads like."""
        ticket = fixtures.create_ticket(user=self.user, title="bgp down", payload={"peer": "10.0.0.1"})

        run, _ = self.run_agent("Nothing to report.", ticket=ticket)

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("bgp down", prompt)
        self.assertIn("10.0.0.1", prompt)
        self.assertIn("data and not instructions", prompt)

    def test_an_attached_device_carries_its_platform_into_the_prompt(self):
        """An agent that does not know the platform reaches for the syntax it knows best.

        Observed against the lab: told `device_type: nokia_srl` by a tool call, the model went on
        asking an SR Linux box for `show ip interface brief` and `show bgp summary` until it ran
        out of iterations. Nautobot knows what the device is; this is the app saying so.
        """

        device = fixtures.create_device(name="edge-01")
        ticket_service.attach_object(ticket=self.ticket, obj=device, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("edge-01", prompt)
        self.assertIn(str(device.device_type), prompt)
        self.assertIn(str(device.device_type.manufacturer), prompt)

    def test_an_attached_object_with_no_platform_reads_as_before(self):
        """Only a device gains the extra clause; everything else is untouched."""

        location = fixtures.create_location()
        ticket_service.attach_object(ticket=self.ticket, obj=location, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        self.assertIn(f"dcim.location: {location.name}", self.messages(run, "user")[0]["content"])

    def test_a_huge_payload_is_capped(self):
        """The payload is somebody else's, and it does not get to be the whole context."""
        ticket = fixtures.create_ticket(user=self.user, title="noisy", payload={"blob": "x" * 50_000})

        with fixtures.agent_settings(max_context_chars=200):
            run = agent_service.run_agent(
                ticket=ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete("Nothing to report."),
            )

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("…(truncated)", prompt)
        self.assertLess(len(prompt), 2000)

    def test_the_prompt_names_the_objects_enrichment_attached(self):
        """Phase 4A put them there; this is what they were for."""
        location = fixtures.create_location()

        ticket_service.attach_object(ticket=self.ticket, obj=location, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        self.assertIn(location.name, self.messages(run, "user")[0]["content"])

    def test_the_agent_s_own_failures_are_kept_out_of_the_next_prompt(self):
        """A run reading its own past failure off the ticket diagnoses the app, not the fault.

        Observed rather than theorised: on a misconfigured endpoint the first run left "the agent
        run failed: ... Missing credentials", and the next run reported a missing OpenAI key as the
        root cause of an interface being down - then left that conclusion for the run after it.
        """
        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("Missing credentials")),
            )

        run, _ = self.run_agent("Nothing to report.")

        prompt = self.messages(run, "user")[0]["content"]
        self.assertNotIn("Missing credentials", prompt)
        self.assertNotIn(agent_service.FAILURE_COMMENT_PREFIX, prompt)

    def test_a_bound_report_is_kept_out_too(self):
        """ "The agent stopped at the limit of 3 model calls" is not a fact about the network."""
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        self.run_agent([fixtures.fake_tool_call("look")])

        run, _ = self.run_agent("Nothing to report.")

        self.assertNotIn(agent_service.BOUND_COMMENT_PREFIX, self.messages(run, "user")[0]["content"])

    def test_a_previous_conclusion_is_still_carried(self):
        """What a run *found* is history worth having; a second run should build on the first."""
        self.run_agent("The optic on ethernet-1/1 is failing.")

        run, _ = self.run_agent("Agreed.")

        self.assertIn("The optic on ethernet-1/1 is failing.", self.messages(run, "user")[0]["content"])

    def test_a_human_comment_is_still_carried(self):
        """Only the agent's reports about itself are filtered, never anybody else's words."""

        ticket_service.add_comment(
            ticket=self.ticket,
            message="The agent run failed: I am a human quoting the agent.",
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )

        run, _ = self.run_agent("Noted.")

        # Matched on the prefix, so a person quoting it is filtered too. Acceptable: the cost is a
        # dropped line in one prompt, and the alternative is a marker field on every update row.
        self.assertNotIn("I am a human quoting the agent", self.messages(run, "user")[0]["content"])

    def test_an_empty_answer_fails_the_run(self):
        """An empty answer is a failed call (L4), so the run fails where an operator sees it."""
        run, _ = self.run_agent("   ")

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn("The model call failed", self.updates(UpdateTypeChoices.COMMENT)[-1].message)


class TestTheToolsOnOffer(AgentTestCase):
    """Rule M4 as the model sees it: what is in the tool list, and what is not."""

    def test_only_enabled_tools_on_enabled_servers_are_offered(self):
        """A server advertising forty tools grants forty times nothing."""
        server = fixtures.create_mcpserver()
        fixtures.create_mcptool(server=server, name="allowed", enabled=True, writable=False)
        fixtures.create_mcptool(server=server, name="not_enabled")
        disabled_server = fixtures.create_mcpserver(name="Off Server", enabled=False)
        fixtures.create_mcptool(server=disabled_server, name="on_a_disabled_server", enabled=True, writable=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(complete.offered_tools, ["allowed"])

    def test_a_tool_on_a_transport_this_app_cannot_call_is_not_offered(self):
        """A call to a tool on an `sse` server is always refused, so the model does not see it."""
        sse_server = fixtures.create_mcpserver(name="SSE Server", transport=MCPTransportChoices.TYPE_SSE)
        fixtures.create_mcptool(server=sse_server, name="on_an_sse_server", enabled=True, writable=False)
        fixtures.create_mcptool(name="on_streamable_http", enabled=True, writable=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(complete.offered_tools, ["on_streamable_http"])

    def test_a_writable_tool_is_offered_and_labelled(self):
        """It is offered, because proposing it is the point. It is labelled for the same reason."""
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

        _, complete = self.run_agent("Nothing to report.")

        definition = complete.calls[-1]["tools"][0]["function"]
        self.assertEqual(definition["name"], "push_config")
        self.assertIn("proposes it to a person", definition["description"])

    def test_two_servers_offering_one_name_get_two_names(self):
        """Providers key tool calls by name, so two of them cannot be the same name."""
        first = fixtures.create_mcpserver(name="Server A")
        second = fixtures.create_mcpserver(name="Server B")
        fixtures.create_mcptool(server=first, name="show", enabled=True, writable=False)
        fixtures.create_mcptool(server=second, name="show", enabled=True, writable=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(sorted(complete.offered_tools), ["show", "show_2"])

    def test_a_name_a_provider_would_refuse_is_rewritten(self):
        """A server may name a tool anything; the OpenAI schema may not."""
        fixtures.create_mcptool(name="show interface (all)", enabled=True, writable=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(complete.offered_tools, ["show_interface__all_"])

    def test_no_tools_means_no_tools_argument(self):
        """A deployment with nothing enabled makes a plain completion, not an empty tool list."""
        _, complete = self.run_agent("Nothing to report.")

        self.assertIsNone(complete.calls[-1]["tools"])


class TestReadOnlyCalls(AgentTestCase):
    """13.4 - reading needs no approval, and what came back goes to the model."""

    def setUp(self):
        """One enabled read-only tool for the model to reach for."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="get_interface_status", enabled=True, writable=False)

    def test_a_read_only_call_runs_and_its_answer_reaches_the_model(self):
        """The loop's step 6, end to end."""
        caller = fixtures.FakeToolCaller(text="ethernet-1/1 is down")

        run, complete = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "It is down.",
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(caller.calls[0]["tool_call"].tool, self.tool)
        self.assertEqual(self.messages(run, "tool")[0]["content"], "ethernet-1/1 is down")
        self.assertEqual(len(complete.calls), 2)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_a_read_only_call_that_the_deadline_stops_goes_to_the_model(self):
        """AC-12 - the run goes on, the model reads the error, and the trail has one entry for the call."""
        error = MCPDeadlineError("The call did not finish in 60 seconds.", call_sent=True)

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Done.",
            call_tool=_wire_with(error),
        )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(
            self.messages(run, "tool")[0]["content"], "The call failed: The call did not finish in 60 seconds."
        )
        entries = self.updates(UpdateTypeChoices.TOOL_EXECUTED)
        self.assertEqual(len(entries), 1)
        self.assertIn("(failed,", entries[0].message)

    def test_a_shortened_result_reaches_the_model_with_a_note(self):
        """The model reads one note line before text that the app made short, and the record keeps the text."""
        caller = fixtures.FakeToolCaller(text="---", separators_shortened=True)

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Done.",
            call_tool=caller,
        )

        self.assertEqual(self.messages(run, "tool")[0]["content"], agent_service.SHORTENED_NOTE + "\n---")
        self.assertEqual(caller.calls[0]["tool_call"].result["text"], "---")

    def test_a_result_that_was_not_shortened_has_no_note(self):
        """Text with no marker key reaches the model as it is."""
        caller = fixtures.FakeToolCaller(text="---")

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Done.",
            call_tool=caller,
        )

        self.assertEqual(self.messages(run, "tool")[0]["content"], "---")

    def test_the_call_is_recorded(self):
        """M7 - the row carries the arguments, whatever else happens."""
        self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Done.",
        )

        record = AgentToolCall.objects.get()
        self.assertEqual(record.arguments, {"device": "leaf-01"})
        self.assertEqual(record.status, AgentToolCallStatusChoices.EXECUTED)
        self.assertIsNone(record.decided_by)

    def test_the_call_reaches_the_ticket_s_trail(self):
        """7.4 - "what has been done to this ticket" includes every call against the network."""
        self.run_agent([fixtures.fake_tool_call("get_interface_status")], "Done.")

        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[-1]
        self.assertIn("get_interface_status", entry.message)
        self.assertEqual(entry.source, TicketSourceChoices.AI)

    def test_the_fingerprint_is_recorded_with_the_call(self):
        """M6's second half needs the digest the call was made against."""

        self.tool.definition_fingerprint = "abc123"
        self.tool.validated_save()

        self.run_agent([fixtures.fake_tool_call("get_interface_status")], "Done.")

        # The binding, not the bare digest: what the call is bound to is the definition, the name
        # and the endpoint together.
        self.assertEqual(AgentToolCall.objects.get().tool_fingerprint, mcp_service.call_binding(self.tool))

    def test_a_tool_the_model_invented_is_answered_rather_than_called(self):
        """M4 from the other side: there is nothing to look up, so nothing is called."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("delete_everything")],
            "Understood.",
            call_tool=caller,
        )

        self.assertEqual(caller.calls, [])
        self.assertIn("no tool called 'delete_everything'", self.messages(run, "tool")[0]["content"])
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_every_read_only_call_of_a_turn_runs_in_order(self):
        """Two reads in one turn are two calls, two rows and two trail entries."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"}, identifier="call-1"),
                fixtures.fake_tool_call("get_interface_status", {"device": "leaf-02"}, identifier="call-2"),
            ],
            "Done.",
            call_tool=caller,
        )

        self.assertEqual([call["tool_call"].arguments["device"] for call in caller.calls], ["leaf-01", "leaf-02"])
        self.assertEqual(
            list(AgentToolCall.objects.values_list("status", flat=True)),
            [AgentToolCallStatusChoices.EXECUTED] * 2,
        )
        self.assertEqual(len(self.updates(UpdateTypeChoices.TOOL_EXECUTED)), 2)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_two_identical_calls_of_a_turn_each_run_and_each_get_an_answer(self):
        """The same name and arguments with different identifiers are two calls."""
        caller = fixtures.FakeToolCaller()
        arguments = {"device": "leaf-01"}

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", arguments, identifier="call-1"),
                fixtures.fake_tool_call("get_interface_status", arguments, identifier="call-2"),
            ],
            "Done.",
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(AgentToolCall.objects.count(), 2)
        self.assertEqual(len(self.messages(run, "tool")), 2)

    def test_each_call_of_a_turn_is_answered_with_its_own_result_in_order(self):
        """No answer starts with "Not executed", and the next request carries both answers."""
        run, complete = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"}, identifier="call-1"),
                fixtures.fake_tool_call("get_interface_status", {"device": "leaf-02"}, identifier="call-2"),
            ],
            "Done.",
            call_tool=_EchoingToolCaller(),
        )

        answers = self.messages(run, "tool")
        self.assertEqual(
            [(answer["tool_call_id"], answer["content"]) for answer in answers],
            [("call-1", "status of leaf-01"), ("call-2", "status of leaf-02")],
        )
        self.assertFalse(any(answer["content"].startswith("Not executed") for answer in answers))
        self.assertEqual(self.messages_of(complete.calls[1]["messages"], "tool"), answers)

    def test_an_unknown_tool_between_two_valid_calls_is_answered_and_the_others_run(self):
        """The unknown name writes no row and does not stop the calls after it."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", identifier="call-1"),
                fixtures.fake_tool_call("delete_everything", identifier="call-2"),
                fixtures.fake_tool_call("get_interface_status", identifier="call-3"),
            ],
            "Done.",
            call_tool=caller,
        )

        answers = {answer["tool_call_id"]: answer["content"] for answer in self.messages(run, "tool")}
        self.assertIn("no tool called 'delete_everything'", answers["call-2"])
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(AgentToolCall.objects.count(), 2)

    def test_a_failed_call_does_not_stop_the_next_call_of_the_turn(self):
        """The model reads each failure, and the agent still makes the next call."""
        caller = fixtures.FakeToolCaller(error=MCPConfigurationError("Tool 'x' is not enabled."))

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", identifier="call-1"),
                fixtures.fake_tool_call("get_interface_status", identifier="call-2"),
            ],
            "I could not read them.",
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 2)
        self.assertTrue(all("The call failed" in answer["content"] for answer in self.messages(run, "tool")))
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_a_failing_tool_is_reported_to_the_model_rather_than_ending_the_run(self):
        """7.4 - the server was reached and had something to say, and it is the model's to read."""
        caller = fixtures.FakeToolCaller(error=MCPConfigurationError("Tool 'x' is not enabled."))

        run, complete = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status")],
            "I could not read it.",
            call_tool=caller,
        )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("not enabled", self.messages(run, "tool")[0]["content"])
        self.assertEqual(len(complete.calls), 2)


class TestTheGate(AgentTestCase):
    """A3 and M6 - the loop ends at the first mutating proposal, and calls nothing."""

    def setUp(self):
        """One enabled mutating tool, which is the whole point of the gate."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

    def propose(self, arguments=None):
        """Run until the model asks for the mutating tool."""
        caller = fixtures.FakeToolCaller()
        run, _ = self.run_agent(
            [fixtures.fake_tool_call("push_config", arguments or {"device": "leaf-01"})],
            call_tool=caller,
        )
        return run, caller

    def test_reads_before_a_mutating_call_run_and_nothing_after_it_does(self):
        """A3 - the first mutating call is proposed, and the calls around it are not made."""
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("look", identifier="call-1"),
                fixtures.fake_tool_call("push_config", identifier="call-2"),
                fixtures.fake_tool_call("look", identifier="call-3"),
            ],
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(AgentToolCall.objects.filter(status=AgentToolCallStatusChoices.PROPOSED).count(), 1)
        self.assertEqual(run.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual([answer["tool_call_id"] for answer in self.messages(run, "tool")], ["call-1"])

    def test_two_mutating_calls_of_a_turn_give_one_proposal(self):
        """One approval gives one change, so the gate holds one proposal."""
        caller = fixtures.FakeToolCaller()

        self.run_agent(
            [
                fixtures.fake_tool_call("push_config", identifier="call-1"),
                fixtures.fake_tool_call("push_config", identifier="call-2"),
            ],
            call_tool=caller,
        )

        self.assertEqual(AgentToolCall.objects.filter(status=AgentToolCallStatusChoices.PROPOSED).count(), 1)
        self.assertEqual(caller.calls, [])

    def test_nothing_is_called(self):
        """The one assertion this phase exists for."""
        _, caller = self.propose()

        self.assertEqual(caller.calls, [])

    def test_the_run_ends_waiting_for_a_person(self):
        """A3 - it never waits, it ends. The worker slot goes back."""
        run, _ = self.propose()

        self.assertEqual(run.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertIsNotNone(run.finished_at)

    def test_the_proposal_is_written_with_the_arguments_as_asked(self):
        """7.3 - approving approves what was proposed, so what was proposed is stored."""
        self.propose({"device": "leaf-01", "config": "shutdown"})

        proposal = AgentToolCall.objects.get()
        self.assertEqual(proposal.status, AgentToolCallStatusChoices.PROPOSED)
        self.assertEqual(proposal.arguments, {"device": "leaf-01", "config": "shutdown"})

    def test_a_proposal_after_an_outcome_unknown_call_of_the_tool_warns_the_approver(self):
        """AC-15 - the entry of a new proposal says that an earlier call of the tool has no known outcome."""
        earlier = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.COMPLETED)
        fixtures.create_agenttoolcall(run=earlier, tool=self.tool, status=AgentToolCallStatusChoices.OUTCOME_UNKNOWN)

        self.propose()

        entry = self.updates(UpdateTypeChoices.TOOL_PROPOSED)[-1]
        self.assertTrue(entry.message.endswith(ticket_service.EARLIER_OUTCOME_UNKNOWN_NOTE))
        self.assertIn("Nothing has been called", entry.message)

    def test_a_proposal_with_no_outcome_unknown_call_of_the_tool_has_no_warning(self):
        """AC-15 - another status, another tool, or another ticket gives no sentence."""
        other_tool = fixtures.create_mcptool(name="reload_device", enabled=True, writable=True)
        other_ticket = fixtures.create_ticket(user=self.user, title="spine-01 bgp down")
        cases = {
            "no earlier call": lambda run: None,
            "earlier failed call": lambda run: fixtures.create_agenttoolcall(
                run=run, tool=self.tool, status=AgentToolCallStatusChoices.FAILED
            ),
            "other tool": lambda run: fixtures.create_agenttoolcall(
                run=run, tool=other_tool, status=AgentToolCallStatusChoices.OUTCOME_UNKNOWN
            ),
            "other ticket": lambda run: fixtures.create_agenttoolcall(
                run=fixtures.create_agentrun(ticket=other_ticket, status=AgentRunStatusChoices.COMPLETED),
                tool=self.tool,
                status=AgentToolCallStatusChoices.OUTCOME_UNKNOWN,
            ),
        }
        for name, make_earlier_call in cases.items():
            with self.subTest(name):
                ticket = fixtures.create_ticket(user=self.user, title=f"leaf-02 {name}")
                make_earlier_call(fixtures.create_agentrun(ticket=ticket, status=AgentRunStatusChoices.COMPLETED))
                if name == "other ticket":
                    ticket = fixtures.create_ticket(user=self.user, title="leaf-03 other ticket")

                self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})], ticket=ticket)

                entry = ticket.updates.filter(update_type=UpdateTypeChoices.TOOL_PROPOSED).last()
                self.assertTrue(entry.message.endswith("this needs a human decision."))
                self.assertNotIn("Outcome Unknown", entry.message)

    def test_the_ticket_s_trail_names_the_server_the_tool_and_the_arguments(self):
        """7.1 - the gate is only as good as the person reading it (section 11)."""
        self.propose({"device": "leaf-01"})

        entry = self.updates(UpdateTypeChoices.TOOL_PROPOSED)[-1]
        self.assertIn("push_config", entry.message)
        self.assertIn(self.tool.mcp_server.name, entry.message)
        self.assertIn("leaf-01", entry.message)
        self.assertIn("Nothing has been called", entry.message)

    def test_the_proposal_is_not_answered_in_the_transcript(self):
        """The resumption answers it with the result of the call it made."""
        run, _ = self.propose()

        self.assertEqual(self.messages(run, "tool"), [])

    def test_a_writable_tool_is_proposed_even_when_the_server_claims_it_reads(self):
        """4.2 - the hint is recorded and decides nothing."""
        self.tool.advertised_read_only = True
        self.tool.validated_save()

        _, caller = self.propose()

        self.assertEqual(caller.calls, [])
        self.assertTrue(AgentToolCall.objects.filter(status=AgentToolCallStatusChoices.PROPOSED).exists())


class TestDecisions(AgentTestCase):
    """7.3 - a decision needs a person, is made once, and lands on the trail."""

    def setUp(self):
        """A run waiting on one proposal."""
        super().setUp()
        self.run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, writable=True)
        self.call = fixtures.create_agenttoolcall(run=self.run, tool=self.tool, arguments={"device": "leaf-01"})

    def test_approving_records_who_and_when(self):
        """The audit half of the gate."""
        call = agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        self.assertEqual(call.status, AgentToolCallStatusChoices.APPROVED)
        self.assertEqual(call.decided_by, self.user)
        self.assertIsNotNone(call.decided_at)

    def test_approving_writes_one_trail_entry_naming_the_approver(self):
        """A person reading the ticket sees who allowed this."""
        agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        entry = self.updates(UpdateTypeChoices.TOOL_DECIDED)[-1]
        self.assertEqual(entry.source, TicketSourceChoices.HUMAN)
        self.assertEqual(entry.user, self.user)
        self.assertIn("approved", entry.message)
        self.assertIn("leaf-01", entry.message)

    def test_a_call_with_no_known_outcome_is_not_decided_again(self):
        """A call with no answer is final, so approve and deny both refuse it."""
        self.call = fixtures.create_agenttoolcall(
            run=self.run, tool=self.tool, status=AgentToolCallStatusChoices.OUTCOME_UNKNOWN
        )
        decided_before = len(self.updates(UpdateTypeChoices.TOOL_DECIDED))
        for decide in (agent_service.approve_tool_call, agent_service.deny_tool_call):
            with self.subTest(decide=decide.__name__):
                with self.assertRaises(AgentDecisionError):
                    decide(tool_call=self.call, user=self.user)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)
        self.assertIsNone(self.call.decided_by)
        self.assertEqual(len(self.updates(UpdateTypeChoices.TOOL_DECIDED)), decided_before)

    def test_denying_ends_the_run(self):
        """13.8 - a denial is the end of the chain, not the start of a negotiation."""
        agent_service.deny_tool_call(tool_call=self.call, user=self.user)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRunStatusChoices.DENIED)
        self.assertIsNotNone(self.run.finished_at)

    def test_a_read_only_call_in_flight_is_not_offered_for_decision(self):
        """A read-only call is written `proposed` before it runs, and needs no decision (13.4).

        Filtering the gate on status alone surfaced Approve and Deny for a call the agent was
        already making: press Approve in that window and the ticket's trail records a human
        approving something nobody was asked about.
        """
        read_only = fixtures.create_mcptool(name="look", enabled=True, writable=False)
        run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.RUNNING)
        fixtures.create_agenttoolcall(run=run, tool=read_only)

        self.assertIsNone(agent_service.pending_call(run))

    def test_a_stranded_read_only_call_cannot_hide_the_real_proposal(self):
        """The compounding case: the gate must show the mutating call, not the oldest row.

        A read-only row left `proposed` by a failure sorts first by `proposed_at`, so the ticket
        page showed it and the genuine mutating proposal was never offered to anybody.
        """
        read_only = fixtures.create_mcptool(name="look", enabled=True, writable=False)
        run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        fixtures.create_agenttoolcall(run=run, tool=read_only)
        real = fixtures.create_agenttoolcall(run=run, tool=self.tool)

        self.assertEqual(agent_service.pending_call(run), real)

    def test_denying_does_not_rewrite_a_run_that_already_finished(self):
        """A stale proposal on a completed run must not turn that run into a denied one."""
        run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.COMPLETED)
        stale = fixtures.create_agenttoolcall(run=run, tool=self.tool)

        agent_service.deny_tool_call(tool_call=stale, user=self.user)

        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentToolCallStatusChoices.DENIED)

    def test_a_decision_without_a_user_is_refused(self):
        """The AI cannot approve its own proposal, because the function will not let it."""
        with self.assertRaises(AgentDecisionError):
            agent_service.approve_tool_call(tool_call=self.call, user=None)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.PROPOSED)

    def test_a_call_is_decided_once(self):
        """The trail already says what happened; a second decision would not change it."""
        agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        with self.assertRaises(AgentDecisionError) as caught:
            agent_service.deny_tool_call(tool_call=self.call, user=self.user)

        self.assertIn("already", str(caught.exception))


class TestResumption(AgentTestCase):
    """7.4 and 13.5 - the approved call runs, and the model carries on from the transcript."""

    def setUp(self):
        """A run that proposed a mutating call and stopped."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, writable=True)
        self.parent, _ = self.run_agent(
            [fixtures.fake_tool_call("push_config", {"device": "leaf-01"})],
        )
        self.proposal = AgentToolCall.objects.get()

    def resume(self, *turns, call_tool=None):
        """Approve the proposal and run the agent again, which is what the button does."""
        agent_service.approve_tool_call(tool_call=self.proposal, user=self.user)
        return self.run_agent(*turns, call_tool=call_tool)

    def test_the_approved_call_is_made(self):
        """The one thing an approval is for."""
        caller = fixtures.FakeToolCaller(text="config applied")

        run, _ = self.resume("Done.", call_tool=caller)

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(caller.calls[0]["tool_call"].pk, self.proposal.pk)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_another_fault_before_the_claim_fails_the_approved_call(self):
        """AC-7 - a fault that is not the signal gives the same row, and keeps its own run error."""
        run, _ = self.resume("Done.", call_tool=mock.Mock(side_effect=RuntimeError("database gone")))

        self.proposal.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.error, "The run failed unexpectedly: database gone")
        self.assertEqual(self.proposal.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(self.proposal.error, mcp_service.UNSTARTED_CALL_ERROR)
        self.assertEqual(len(self.updates(UpdateTypeChoices.TOOL_EXECUTED)), 1)

    def test_a_run_that_fails_after_the_approved_call_ran_leaves_the_call_executed(self):
        """The mark finds no approved row when the call ran."""
        agent_service.approve_tool_call(tool_call=self.proposal, user=self.user)

        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("boom")),
                call_tool=fixtures.FakeToolCaller(),
            )

        self.proposal.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(self.proposal.status, AgentToolCallStatusChoices.EXECUTED)
        self.assertEqual(len(self.updates(UpdateTypeChoices.TOOL_EXECUTED)), 1)

    def test_a_resumption_stores_its_own_limit_and_leaves_the_parent_alone(self):
        """AC-3 - the resumption is a new row with its own limit, and the parent keeps the limit it had."""
        other = fixtures.create_ticket()
        parent, _ = self.run_agent(
            [fixtures.fake_tool_call("push_config", {"device": "leaf-01"})], ticket=other, time_limit=1200
        )
        agent_service.approve_tool_call(tool_call=AgentToolCall.objects.get(run=parent), user=self.user)

        run, _ = self.run_agent("Done.", ticket=other, time_limit=900)

        parent.refresh_from_db()
        self.assertEqual(run.parent, parent)
        self.assertEqual(run.time_limit_seconds, 900)
        self.assertEqual(parent.time_limit_seconds, 1200)

    def test_the_result_answers_the_pending_call_in_the_transcript(self):
        """The provider requires every tool call to be answered before the next turn."""
        run, _ = self.resume("Done.", call_tool=fixtures.FakeToolCaller(text="config applied"))

        answers = self.messages(run, "tool")
        self.assertEqual(answers[0]["content"], "config applied")

    def test_the_parent_run_is_superseded_rather_than_left_waiting(self):
        """A waiting run that has been taken over is not still waiting on anybody."""
        self.resume("Done.")

        self.parent.refresh_from_db()
        self.assertEqual(self.parent.status, AgentRunStatusChoices.SUPERSEDED)

    def test_the_resumption_knows_which_run_it_continued(self):
        """The chain is what makes a multi-step investigation readable afterwards."""
        run, _ = self.resume("Done.")

        self.assertEqual(run.parent, self.parent)

    def test_the_transcript_is_carried_over(self):
        """13.5 - the transcript is replayed as messages, not as provider state."""
        run, complete = self.resume("Done.")

        self.assertEqual(run.transcript[0]["role"], "system")
        self.assertGreater(len(complete.calls[0]["messages"]), 3)

    def test_an_approved_call_that_the_deadline_stops_warns_on_the_trail(self):
        """The row and the trail entry say that the change might be in effect."""
        error = MCPDeadlineError("The call did not finish in 60 seconds.", call_sent=True)

        run, _ = self.resume("Done.", call_tool=_wire_with(error))

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)
        self.assertIn("the change might be in effect", self.proposal.error)
        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[-1]
        self.assertIn("the change might be in effect", entry.message)
        self.assertIn("(outcome_unknown,", entry.message)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(
            self.messages(run, "tool")[0]["content"],
            "The outcome of the call is not known: The call did not finish in 60 seconds. "
            f"{mcp_service.CHANGE_MIGHT_BE_IN_EFFECT}",
        )
        self.assertEqual(AgentToolCall.objects.count(), 1)

    def test_the_result_reaches_the_ticket_s_trail(self):
        """7.4 - the call happened, and the ticket says so."""
        self.resume("Done.", call_tool=fixtures.FakeToolCaller(text="config applied"))

        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[-1]
        self.assertIn("push_config", entry.message)

    def test_a_resumption_with_no_time_for_the_approved_call_is_refused(self):
        """AC-16, edge case (d) - the call stays approved and no run opens."""
        agent_service.approve_tool_call(tool_call=self.proposal, user=self.user)
        caller = fixtures.FakeToolCaller()

        with self.assertRaises(AgentConfigurationError) as caught:
            self.run_agent("Done.", call_tool=caller, soft_time_limit=80)

        self.proposal.refresh_from_db()
        self.parent.refresh_from_db()
        self.assertEqual(
            str(caught.exception),
            "The run time limit of 80 seconds leaves no time for the approved call, which can take 60 seconds. "
            "The call stays approved. Raise the time limits of the Job or lower `tool_timeout_seconds`, "
            "then run the agent again.",
        )
        self.assertEqual(AgentRun.objects.count(), 1)
        self.assertEqual(self.parent.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual(self.proposal.status, AgentToolCallStatusChoices.APPROVED)
        self.assertEqual(caller.calls, [])

    def test_a_resumption_with_exactly_enough_time_makes_the_approved_call(self):
        """AC-16 - two tool timeouts and the reserve fit in 90 seconds."""
        caller = fixtures.FakeToolCaller(text="config applied")
        agent_service.approve_tool_call(tool_call=self.proposal, user=self.user)

        run, _ = self.run_agent("Done.", call_tool=caller, soft_time_limit=90)

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(caller.calls), 1)

    def test_a_denied_proposal_does_not_resume(self):
        """A denial ends the chain, and the next launch is a fresh run rather than a resumption."""
        agent_service.deny_tool_call(tool_call=self.proposal, user=self.user)

        run, _ = self.run_agent("Starting again.")

        self.assertIsNone(run.parent)


class TestResumptionOfATurnWithMoreCalls(AgentTestCase):
    """A3 and A4 - an approval answers its own call, and the later calls are told they did not run."""

    def setUp(self):
        """One read-only tool and one mutating tool."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

    def approve_and_resume(self, calls):
        """Run one turn of these calls, approve its proposal, and run the agent again."""
        self.run_agent(calls)
        agent_service.approve_tool_call(
            tool_call=AgentToolCall.objects.get(status=AgentToolCallStatusChoices.PROPOSED), user=self.user
        )
        caller = fixtures.FakeToolCaller(text="config applied")
        run, _ = self.run_agent("Done.", call_tool=caller)
        return run, caller

    def test_the_later_call_is_told_it_did_not_run(self):
        """The approved result answers the mutating call, and the last call gets a plain refusal."""
        run, caller = self.approve_and_resume(
            [
                fixtures.fake_tool_call("look", identifier="call-1"),
                fixtures.fake_tool_call("push_config", identifier="call-2"),
                fixtures.fake_tool_call("look", identifier="call-3"),
            ]
        )

        answers = {answer["tool_call_id"]: answer["content"] for answer in self.messages(run, "tool")}
        self.assertEqual(answers["call-2"], "config applied")
        self.assertTrue(answers["call-3"].startswith("Not executed"))
        self.assertIn("approval", answers["call-3"])
        self.assertEqual(len(caller.calls), 1)

    def test_the_second_mutating_call_is_told_it_did_not_run_and_has_no_proposal(self):
        """The second change needs a new request, not a second row."""
        run, _ = self.approve_and_resume(
            [
                fixtures.fake_tool_call("push_config", identifier="call-1"),
                fixtures.fake_tool_call("push_config", identifier="call-2"),
            ]
        )

        answers = {answer["tool_call_id"]: answer["content"] for answer in self.messages(run, "tool")}
        self.assertTrue(answers["call-2"].startswith("Not executed"))
        self.assertEqual(AgentToolCall.objects.count(), 1)

    def test_a_read_and_a_mutating_call_with_one_identifier_still_answer_the_mutating_call(self):
        """The app gives the second call a new identifier, so the approved result finds its call."""
        run, _ = self.approve_and_resume(
            [
                fixtures.fake_tool_call("look", identifier="call-1"),
                fixtures.fake_tool_call("push_config", identifier="call-1"),
            ]
        )

        mutating_id = self.messages(run, "assistant")[0]["tool_calls"][1]["id"]
        answers = {answer["tool_call_id"]: answer["content"] for answer in self.messages(run, "tool")}
        self.assertEqual(answers[mutating_id], "config applied")


class TestCallIdentifiers(AgentTestCase):
    """The app does not trust the provider to give each call a different identifier."""

    def setUp(self):
        """One read-only tool."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)

    def test_empty_and_repeated_identifiers_of_one_turn_are_replaced(self):
        """A good identifier stays, and each tool message uses the stored identifier."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("look", identifier="call-1"),
                fixtures.fake_tool_call("look", identifier="call-1"),
                fixtures.fake_tool_call("look", identifier=""),
            ],
            "Done.",
            call_tool=caller,
        )

        stored = [call["id"] for call in self.messages(run, "assistant")[0]["tool_calls"]]
        self.assertEqual(len(set(stored)), 3)
        self.assertNotIn("", stored)
        self.assertEqual(stored[0], "call-1")
        self.assertEqual([answer["tool_call_id"] for answer in self.messages(run, "tool")], stored)
        self.assertEqual(len(caller.calls), 3)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_an_identifier_of_an_earlier_turn_is_replaced(self):
        """Each tool message agrees with the turn it answers."""
        run, _ = self.run_agent(
            [fixtures.fake_tool_call("look", identifier="call-1")],
            [fixtures.fake_tool_call("look", identifier="call-1")],
            "Done.",
        )

        assistants = self.messages(run, "assistant")
        stored = [message["tool_calls"][0]["id"] for message in assistants[:2]]
        self.assertNotEqual(stored[0], stored[1])
        self.assertEqual([answer["tool_call_id"] for answer in self.messages(run, "tool")], stored)


class TestBounds(AgentTestCase):
    """A2 - the four counted bounds, and each of them ends a run cleanly with what it has."""

    def setUp(self):
        """One read-only tool, so a run can be made to loop."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)

    def test_the_iteration_limit_ends_the_run(self):
        """A model that never stops asking is a bill, and "usually stops" is not a bound."""
        run, complete = self.run_agent([fixtures.fake_tool_call("look")])

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(complete.calls), 3)
        self.assertIn("3 model calls", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_the_tool_call_limit_ends_the_run(self):
        """Counted across the chain, so an approval does not reset it."""
        with fixtures.agent_settings(max_iterations=10, max_tool_calls=2):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("look")]),
                call_tool=fixtures.FakeToolCaller(),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(AgentToolCall.objects.count(), 2)
        self.assertIn("2 tool calls", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_the_tool_call_limit_stops_a_turn_in_the_middle(self):
        """The calls past the limit are not made and are not answered."""
        caller = fixtures.FakeToolCaller()
        with fixtures.agent_settings(max_iterations=10, max_tool_calls=2):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete(
                    [fixtures.fake_tool_call("look", identifier=f"call-{number}") for number in (1, 2, 3)]
                ),
                call_tool=caller,
            )

        self.assertEqual(AgentToolCall.objects.count(), 2)
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual([answer["tool_call_id"] for answer in self.messages(run, "tool")], ["call-1", "call-2"])
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("2 tool calls", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_the_transcript_limit_ends_the_run(self):
        """A long run re-sends its history and pays for it; this is where it stops."""
        with fixtures.agent_settings(max_iterations=10, max_transcript_chars=1):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("look")]),
                call_tool=fixtures.FakeToolCaller(),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("transcript limit", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_a_bounded_run_keeps_what_it_found(self):
        """ "With what it has" is the half that makes a bound useful rather than a loss."""
        run, _ = self.run_agent([fixtures.fake_tool_call("look")])

        self.assertGreater(len(run.transcript), 3)
        self.assertGreater(AgentToolCall.objects.count(), 0)


class TestTheTimeLimit(AgentTestCase):
    """A2 - the run time limit: a call starts only if its allowance ends 30 seconds before the limit."""

    def setUp(self):
        """One read-only tool, one writable tool, and a clock that a test moves."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)
        self.clock = fixtures.FakeClock()

    def run_limited(self, *turns, seconds=0, **service_kwargs):
        """Run with room for many calls, and with each tool call taking this long on the fake clock."""
        complete = fixtures.FakeAgentComplete(*turns)
        with fixtures.agent_settings(max_iterations=20, max_tool_calls=20):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=complete,
                call_tool=_SlowToolCaller(self.clock, seconds),
                clock=self.clock,
                **service_kwargs,
            )
        return run, complete

    def last_comment(self):
        """The newest comment on the ticket."""
        return self.updates(UpdateTypeChoices.COMMENT)[-1].message

    def test_a_model_call_that_does_not_fit_is_not_started(self):
        """AC-1, AC-3 - the run ends completed and the comment says what was used."""
        run, complete = self.run_limited([fixtures.fake_tool_call("look")], seconds=520)

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(complete.calls), 1)
        self.assertEqual(
            self.last_comment(),
            "The agent stopped at the run time limit (520 of 600 seconds used, and one more model call can "
            "take 60 seconds) without finishing its investigation.",
        )

    def test_a_tool_call_that_does_not_fit_stops_a_turn_in_the_middle(self):
        """AC-2, AC-3 - the later calls of the turn get no answer and no row."""
        calls = [fixtures.fake_tool_call("look", identifier=f"call-{number}") for number in (1, 2, 3)]

        run, _ = self.run_limited(calls, seconds=260)

        self.assertEqual(AgentToolCall.objects.count(), 2)
        self.assertEqual([answer["tool_call_id"] for answer in self.messages(run, "tool")], ["call-1", "call-2"])
        self.assertIn("520 of 600 seconds used, and one more tool call can take 60 seconds", self.last_comment())
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_the_tool_allowance_is_the_deadline_of_the_mcp_service(self):
        """AC-11 - the time check takes its allowance from the MCP service, so the two numbers cannot differ."""
        calls = [fixtures.fake_tool_call("look", identifier=f"call-{number}") for number in (1, 2, 3)]

        with mock.patch.object(mcp_service, "CALL_DEADLINE_TIMEOUTS", 3):
            self.run_limited(calls, seconds=260)

        self.assertIn("520 of 600 seconds used, and one more tool call can take 90 seconds", self.last_comment())

    def test_a_call_that_ends_exactly_30_seconds_before_the_limit_starts(self):
        """AC-1 - 510 + 60 + 30 is 600, which fits."""
        run, complete = self.run_limited([fixtures.fake_tool_call("look")], "Done.", seconds=510)

        self.assertEqual(len(complete.calls), 2)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertNotIn(agent_service.BOUND_COMMENT_PREFIX, self.last_comment())

    def test_a_limit_with_no_room_for_one_model_call_stops_the_run_at_once(self):
        """AC-4, edge case (a) - a limit of 80 seconds leaves no room for a call of 60."""
        run, complete = self.run_limited("Never asked.", soft_time_limit=80)

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(complete.calls, [])
        self.assertEqual(run.iterations, 0)
        self.assertIn("0 of 80 seconds used", self.last_comment())

    def test_a_limit_above_the_defaults_gives_a_run_no_more_time(self):
        """AC-4 - the lowest of the soft limit, the hard limit and 600 seconds."""
        for limits in ({"soft_time_limit": 3000, "time_limit": 3060}, {}):
            with self.subTest(limits=limits):
                self.clock.now = 0
                self.run_limited([fixtures.fake_tool_call("look")], seconds=520, **limits)
                self.assertIn("520 of 600 seconds used", self.last_comment())

    def test_a_hard_limit_below_the_soft_limit_sets_the_run_time_limit(self):
        """AC-4 - only the hard limit is lowered, and the clean stop comes before the kill."""
        self.run_limited([fixtures.fake_tool_call("look")], seconds=250, soft_time_limit=600, time_limit=300)

        self.assertIn("250 of 300 seconds used", self.last_comment())

    def test_a_proposal_is_written_with_no_time_check(self):
        """AC-5, edge case (c) - a proposal makes no network call, so a run near its limit still writes it."""
        calls = [fixtures.fake_tool_call("look", identifier="call-1"), fixtures.fake_tool_call("push_config")]

        run, _ = self.run_limited(calls, seconds=590)

        self.assertEqual(run.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual(AgentToolCall.objects.filter(tool__name="push_config").count(), 1)

    def test_a_resumption_has_its_own_clock(self):
        """AC-5, edge case (d) - the elapsed time starts at the first reading of the run."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        run, _ = self.run_agent("Done.", clock=fixtures.FakeClock(10000))

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertNotIn(agent_service.BOUND_COMMENT_PREFIX, self.last_comment())

    def test_the_stop_comment_is_kept_out_of_the_next_prompt(self):
        """AC-7, edge case (g) - a later run does not read the limit as a fault of the network."""
        self.run_limited([fixtures.fake_tool_call("look")], seconds=520)

        run, _ = self.run_agent("Nothing to report.")

        self.assertNotIn("run time limit", self.messages(run, "user")[0]["content"])

    def test_each_model_call_is_sent_with_no_retry(self):
        """AC-6 - the SDK retry hides the soft time limit and triples the time of a call."""
        _, complete = self.run_limited([fixtures.fake_tool_call("look")], "Done.")

        self.assertEqual([call["max_retries"] for call in complete.calls], [0, 0])


def _wire_with(error):
    """A `call_tool` seam that is the real service over a client that raises this error."""

    def _call_tool(**kwargs):
        return mcp_service.call_tool(**kwargs, client=fixtures.FakeMCPCaller(error=error))

    return _call_tool


def _cut_model_call(**_kwargs):
    """A `complete` seam that fails the way litellm fails when the signal arrives in a call."""
    raise LLMCallError("The call failed.") from SoftTimeLimitExceeded()


class _CuttingToolCaller(fixtures.FakeToolCaller):  # pylint: disable=too-few-public-methods
    """A tool seam that claims its row and is then cut by the signal."""

    def __call__(self, *, tool_call, timeout=None, max_result_chars=None):
        """Mark the row executing, as the claim does, and raise the signal."""
        AgentToolCall.objects.filter(pk=tool_call.pk).update(status=AgentToolCallStatusChoices.EXECUTING)
        raise SoftTimeLimitExceeded()


class _SignalToolCaller(fixtures.FakeToolCaller):  # pylint: disable=too-few-public-methods
    """A tool seam that sets the row to this status, if given, and then raises the signal."""

    def __init__(self, row_status=None):
        """Leave the row alone when no status is given."""
        super().__init__()
        self.row_status = row_status

    def __call__(self, *, tool_call, timeout=None, max_result_chars=None):
        """Write the status, then raise the signal."""
        if self.row_status is not None:
            AgentToolCall.objects.filter(pk=tool_call.pk).update(status=self.row_status)
        raise SoftTimeLimitExceeded()


class TestTheSoftTimeLimit(AgentTestCase):
    """A6 - a run that the soft time limit interrupts ends failed and names the limit."""

    CUT = (
        "The Job reached its soft time limit of {limit} seconds in the middle of {activity}. "
        "Lower `timeout_seconds` or `tool_timeout_seconds`, or raise the time limits of the Job."
    )

    def setUp(self):
        """One read-only tool and one writable tool."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

    def last_comment(self):
        """The newest comment on the ticket."""
        return self.updates(UpdateTypeChoices.COMMENT)[-1].message

    def test_a_signal_in_a_tool_call_fails_the_run(self):
        """AC-9, AC-10 - the real service records the row and raises the signal again."""
        run, complete = self.run_agent([fixtures.fake_tool_call("look")], call_tool=_wire_with(SoftTimeLimitExceeded()))

        text = self.CUT.format(limit=600, activity="a tool call")
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.error, text)
        self.assertEqual(self.last_comment(), f"{agent_service.FAILURE_COMMENT_PREFIX}{text}")
        self.assertEqual(len(complete.calls), 1)
        self.assertEqual(AgentToolCall.objects.get().error, mcp_service.SOFT_TIME_LIMIT_CALL_ERROR)

    def test_a_signal_in_a_model_call_fails_the_run(self):
        """AC-10 - litellm wraps the signal, and the limit that the Job passed is named."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket, user=self.user, complete=_cut_model_call, soft_time_limit=200
            )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.error, self.CUT.format(limit=200, activity="a model call"))

    def test_a_model_error_with_no_signal_keeps_its_text(self):
        """AC-10 - only the signal changes the text."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket, user=self.user, complete=_failing_complete(LLMCallError("boom"))
            )

        self.assertEqual(run.error, "The model call failed: boom")

    def test_a_signal_in_other_work_fails_the_run(self):
        """AC-10 - the signal can arrive anywhere in the loop."""
        with mock.patch.object(ticket_service, "get_related_objects", side_effect=SoftTimeLimitExceeded()):
            run, complete = self.run_agent("Never asked.")

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.error, self.CUT.format(limit=600, activity="its work"))
        self.assertEqual(complete.calls, [])

    def test_a_row_left_executing_by_the_signal_becomes_failed(self):
        """AC-11 - a call that was cut before its row was written."""
        run, _ = self.run_agent([fixtures.fake_tool_call("look")], call_tool=_CuttingToolCaller())

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.SOFT_TIME_LIMIT_CALL_ERROR)

    def test_the_activity_is_reset_after_a_tool_call_that_failed(self):
        """AC-10 - a fault in a tool call must not leave the next model call named as a tool call."""
        complete = fixtures.FakeAgentComplete([fixtures.fake_tool_call("look")])
        calls = []

        def _signal_on_second_call(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise SoftTimeLimitExceeded()
            return complete(**kwargs)

        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_signal_on_second_call,
                call_tool=fixtures.FakeToolCaller(error=mcp_service.MCPCallError("no route")),
            )

        self.assertEqual(run.error, self.CUT.format(limit=600, activity="a model call"))

    def test_a_cut_approved_call_of_the_parent_says_the_change_might_be_in_effect(self):
        """AC-11, AC-13 - the sweep reads the whole chain, and the comment ends with the warning."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        run, _ = self.run_agent("Done.", call_tool=_CuttingToolCaller())

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)
        self.assertEqual(row.error, mcp_service.CUT_CHANGE_ERROR)
        self.assertTrue(run.error.endswith(mcp_service.CUT_CHANGE_RUN_NOTE))
        self.assertTrue(self.last_comment().endswith(mcp_service.CUT_CHANGE_RUN_NOTE))
        self.assertIn(AgentToolCallStatusChoices.as_dict()["outcome_unknown"], mcp_service.CUT_CHANGE_RUN_NOTE)

    def test_a_cut_approved_call_that_the_service_recorded_still_warns(self):
        """AC-13 - the real `call_tool` writes the row, so the run adds the warning from the call in flight."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        run, _ = self.run_agent("Done.", call_tool=_wire_with(SoftTimeLimitExceeded()))

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)
        self.assertEqual(row.error, mcp_service.CUT_CHANGE_ERROR)
        self.assertTrue(run.error.endswith(mcp_service.CUT_CHANGE_RUN_NOTE))
        self.assertTrue(self.last_comment().endswith(mcp_service.CUT_CHANGE_RUN_NOTE))

    def test_a_signal_after_the_service_wrote_outcome_unknown_still_warns(self):
        """AC-8, AC-7 - the signal comes after the service wrote the status and before the loop recorded it."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        run, _ = self.run_agent("Done.", call_tool=_SignalToolCaller(AgentToolCallStatusChoices.OUTCOME_UNKNOWN))

        entries = self.updates(UpdateTypeChoices.TOOL_EXECUTED)
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn(mcp_service.CUT_CHANGE_RUN_NOTE, run.error)
        self.assertEqual(len(entries), 1)
        self.assertIn("(outcome_unknown,", entries[0].message)
        self.assertEqual(AgentToolCall.objects.get().status, AgentToolCallStatusChoices.OUTCOME_UNKNOWN)

    def test_the_note_stays_complete_before_an_earlier_cause(self):
        """AC-8 - the cap of the run error cuts the earlier cause and never the note."""
        first_cause = "x" * 2000
        text = agent_service._soft_time_limit_message(  # pylint: disable=protected-access
            limit=600, activity="a tool call", cut_change=True, first_cause=first_cause
        )

        kept = text[: mcp_service.ERROR_TEXT_CAP]
        end_of_note = kept.index(mcp_service.CUT_CHANGE_RUN_NOTE) + len(mcp_service.CUT_CHANGE_RUN_NOTE)
        self.assertLess(end_of_note, 500)
        self.assertTrue(kept[end_of_note:].startswith(agent_service.SOFT_TIME_LIMIT_FIRST_CAUSE))
        alone = agent_service._soft_time_limit_message(  # pylint: disable=protected-access
            limit=600, activity="a tool call", cut_change=True, first_cause=""
        )
        self.assertTrue(alone.endswith(mcp_service.CUT_CHANGE_RUN_NOTE))
        plain = agent_service._soft_time_limit_message(  # pylint: disable=protected-access
            limit=600, activity="a tool call", cut_change=False, first_cause=""
        )
        self.assertNotIn(mcp_service.CUT_CHANGE_RUN_NOTE, plain)

    def approved_state(self):
        """Run the agent to a proposal, approve it, and return the parent run."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)
        return parent

    def test_an_approved_call_that_the_signal_reached_before_its_claim_is_failed(self):
        """AC-1, AC-3, AC-5, AC-6, AC-12 - the call did not start, so it is failed and the run text does not change."""
        parent = self.approved_state()

        run, _ = self.run_agent("Done.", call_tool=_SignalToolCaller())

        row = AgentToolCall.objects.get()
        parent.refresh_from_db()
        text = self.CUT.format(limit=600, activity="a tool call")
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(parent.status, AgentRunStatusChoices.SUPERSEDED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)
        self.assertIsNone(row.called_at)
        self.assertEqual(run.error, text)
        self.assertEqual(self.last_comment(), f"{agent_service.FAILURE_COMMENT_PREFIX}{text}")

    def test_a_signal_in_the_claim_of_the_real_service_fails_the_approved_call(self):
        """AC-1, AC-12 - the signal arrives in the real `call_tool`, before its claim."""
        self.approved_state()

        with mock.patch.object(mcp_service, "_claim", side_effect=SoftTimeLimitExceeded()):
            run, _ = self.run_agent("Done.", call_tool=mcp_service.call_tool)

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)

    def test_a_signal_before_the_tool_call_starts_fails_the_approved_call(self):
        """The signal arrives before `_execute` starts."""
        self.approved_state()

        with mock.patch.object(agent_service._Loop, "_execute", side_effect=SoftTimeLimitExceeded()):  # pylint: disable=protected-access
            run, _ = self.run_agent("Done.")

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(run.error, self.CUT.format(limit=600, activity="its work"))
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)

    def test_the_next_launch_after_an_unstarted_call_starts_a_fresh_run(self):
        """AC-9 - no run makes the call, and the prompt carries the trail entry."""
        self.approved_state()
        self.run_agent("Done.", call_tool=_SignalToolCaller())
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent("Looked again.", call_tool=caller)

        self.assertIsNone(run.parent)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(caller.calls, [])
        self.assertIn(mcp_service.UNSTARTED_CALL_ERROR, self.messages(run, "user")[0]["content"])

    def test_a_ticket_resolved_before_the_claim_still_fails_the_approved_call(self):
        """The trail refuses the entry of a resolved ticket, and the row is failed all the same."""
        self.approved_state()

        def _resolve_then_signal(**_kwargs):
            ticket_service.walk_to_status(
                ticket=self.ticket,
                to_status=TicketStatusChoices.RESOLVED,
                source=TicketSourceChoices.HUMAN,
                user=self.user,
                resolution="Fixed in tests.",
            )
            raise SoftTimeLimitExceeded()

        with self.assertLogs("nautobot_event_tracker.services.agent", "WARNING"):
            run, _ = self.run_agent("Done.", call_tool=_resolve_then_signal)

        row = AgentToolCall.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)
        self.assertEqual(self.updates(UpdateTypeChoices.TOOL_EXECUTED), [])

    def test_a_signal_in_the_mark_of_an_unstarted_call_keeps_the_first_cause(self):
        """The first cause stays in the run error when the signal arrives in the mark."""
        self.approved_state()
        mark = mcp_service.fail_unstarted_calls
        calls = []

        def _signal_once(run_ids):
            calls.append(run_ids)
            if len(calls) == 1:
                raise SoftTimeLimitExceeded()
            return mark(run_ids)

        with mock.patch.object(mcp_service, "fail_unstarted_calls", side_effect=_signal_once):
            run, _ = self.run_agent("Done.", call_tool=mock.Mock(side_effect=RuntimeError("boom")))

        row = AgentToolCall.objects.get()
        self.assertTrue(run.error.startswith(self.CUT.format(limit=600, activity="a tool call")))
        self.assertIn("The run was already ending for this cause: The run failed unexpectedly: boom", run.error)
        self.assertEqual(row.status, AgentToolCallStatusChoices.FAILED)
        self.assertEqual(row.error, mcp_service.UNSTARTED_CALL_ERROR)

    def test_a_call_that_finished_before_the_signal_carries_no_warning(self):
        """AC-13 - a row that is executed was not cut."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        run, _ = self.run_agent("Done.", call_tool=_SignalToolCaller(AgentToolCallStatusChoices.EXECUTED))

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertNotIn("might be in effect", run.error)
        self.assertEqual(AgentToolCall.objects.get().status, AgentToolCallStatusChoices.EXECUTED)

    def test_a_signal_after_the_end_status_keeps_the_status(self):
        """AC-12, edge case (e) - the run was already completed."""
        finish = agent_service._Loop._finish  # pylint: disable=protected-access
        raised = []

        def _finish_then_signal(loop, status):
            finish(loop, status)
            if not raised:
                raised.append(True)
                raise SoftTimeLimitExceeded()

        with mock.patch.object(agent_service._Loop, "_finish", autospec=True, side_effect=_finish_then_signal):  # pylint: disable=protected-access
            run, _ = self.run_agent("Done.")

        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertNotIn(agent_service.FAILURE_COMMENT_PREFIX, self.last_comment())

    def test_a_signal_while_the_run_handles_another_fault_keeps_both_causes(self):
        """AC-12, edge case (f) - the first cause stays in the text."""
        add_comment = ticket_service.add_comment
        calls = []

        def _signal_once(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise SoftTimeLimitExceeded()
            return add_comment(**kwargs)

        with mock.patch.object(ticket_service, "add_comment", side_effect=_signal_once):
            with fixtures.agent_settings():
                run = agent_service.run_agent(
                    ticket=self.ticket, user=self.user, complete=_failing_complete(LLMCallError("boom"))
                )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertTrue(run.error.startswith(self.CUT.format(limit=600, activity="a model call")))
        self.assertIn("The run was already ending for this cause: The model call failed: boom", run.error)


class TestACutCall(AgentTestCase):
    """AC-14 - a call that a stop cut gets an entry in the ticket's trail."""

    def setUp(self):
        """One read-only tool and one writable tool."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, writable=False)
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

    def test_a_read_only_call_that_the_signal_cut_is_in_the_trail(self):
        """The entry names the call, says it failed, and carries the error of the row."""
        self.run_agent([fixtures.fake_tool_call("look")], call_tool=_wire_with(SoftTimeLimitExceeded()))

        entries = self.updates(UpdateTypeChoices.TOOL_EXECUTED)
        self.assertEqual(len(entries), 1)
        self.assertIn("Called 'look'", entries[0].message)
        self.assertIn("(failed,", entries[0].message)
        self.assertIn(mcp_service.SOFT_TIME_LIMIT_CALL_ERROR, entries[0].message)

    def test_a_cut_approved_call_is_in_the_trail_with_the_warning(self):
        """The entry of a writable call carries the text that the change might be in effect."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        self.run_agent("Done.", call_tool=_CuttingToolCaller())

        entries = self.updates(UpdateTypeChoices.TOOL_EXECUTED)
        self.assertEqual(len(entries), 1)
        self.assertIn("(outcome_unknown,", entries[0].message)
        self.assertIn(mcp_service.CUT_CHANGE_ERROR, entries[0].message)

    def test_an_approved_call_that_was_not_started_has_one_entry(self):
        """AC-4 - the trail says that the call failed, and why."""
        parent, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=agent_service.pending_call(parent), user=self.user)

        self.run_agent("Done.", call_tool=_SignalToolCaller())

        entries = self.updates(UpdateTypeChoices.TOOL_EXECUTED)
        self.assertEqual(len(entries), 1)
        self.assertIn("Called 'push_config'", entries[0].message)
        self.assertIn("(failed, 0 ms)", entries[0].message)
        self.assertIn(mcp_service.UNSTARTED_CALL_ERROR, entries[0].message)


class TestFailure(AgentTestCase):
    """A6 - fail closed. Nothing half-finished is reported as finished."""

    def test_a_model_error_ends_the_run_and_says_so(self):
        """Triage fails open because a missing verdict costs a ticket; this does not."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("the provider refused")),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn("the provider refused", run.error)
        self.assertIn("failed", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_a_failed_run_proposes_nothing(self):
        """The gate is not reached by failing at it."""
        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("boom")),
            )

        self.assertFalse(AgentToolCall.objects.exists())

    def test_an_unexpected_error_ends_the_run_rather_than_escaping(self):
        """Anything at all ends the run visibly, including a fault nobody anticipated."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(RuntimeError("something nobody expected")),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn("something nobody expected", run.error)

    def test_a_ticket_resolved_mid_run_does_not_lose_the_run_s_record(self):
        """S3 refuses the write, which is correct, and the transcript survives it."""

        class _ResolvingComplete(fixtures.FakeAgentComplete):
            """Resolves the ticket between the call and the conclusion."""

            def __init__(self, ticket, user, *turns):
                super().__init__(*turns)
                self.ticket = ticket
                self.user = user

            def __call__(self, **kwargs):
                response = super().__call__(**kwargs)
                ticket_service.walk_to_status(
                    ticket=self.ticket,
                    to_status=TicketStatusChoices.RESOLVED,
                    source=TicketSourceChoices.HUMAN,
                    user=self.user,
                    resolution="Somebody else fixed it.",
                )
                return response

        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_ResolvingComplete(self.ticket, self.user, "Found it."),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(run.transcript), 3)


class TestTheTranscriptShape(AgentTestCase):
    """13.5 - stored as provider-shaped messages, so a provider change does not invalidate it."""

    def test_an_assistant_turn_carries_its_tool_calls_as_the_provider_sent_them(self):
        """The next request has to send them back exactly, ids and all."""
        fixtures.create_mcptool(name="look", enabled=True, writable=False)

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("look", {"device": "leaf-01"}, identifier="call-9")],
            "Done.",
        )

        assistant = self.messages(run, "assistant")[0]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-9")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"device": "leaf-01"})

    def test_the_transcript_is_written_as_the_run_goes(self):
        """A worker killed mid-run leaves something readable, which is the case worth reading."""
        seen = []

        class _WatchingComplete(fixtures.FakeAgentComplete):
            """Records how much of the transcript was persisted before each call."""

            def __call__(self, **kwargs):
                seen.append(AgentRun.objects.get().transcript)
                return super().__call__(**kwargs)

        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket, user=self.user, complete=_WatchingComplete("Nothing to report.")
            )

        self.assertEqual([message["role"] for message in seen[0]], ["system", "user"])


def _failing_complete(error):
    """A `complete` seam that raises this error instead of answering."""

    def _complete(**_kwargs):
        raise error

    return _complete


class TestSessionId(AgentTestCase):
    """Each model call of an investigation sends the ID of its first run as the session ID."""

    def setUp(self):
        """A mutating tool, so that a run can stop at a proposal."""
        super().setUp()
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

    def test_every_call_of_a_run_sends_the_run_id(self):
        """AC-2 - the session of a run is the run."""
        run, complete = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Nothing to report.",
        )

        self.assertGreater(len(complete.calls), 1)
        self.assertEqual({call["session_id"] for call in complete.calls}, {str(run.pk)})

    def test_a_resumed_run_sends_the_id_of_the_first_run(self):
        """AC-2 - an approval continues the investigation, so it continues the session."""
        first, _ = self.run_agent([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        agent_service.approve_tool_call(tool_call=AgentToolCall.objects.get(), user=self.user)

        resumed, complete = self.run_agent("Done.")

        self.assertEqual(resumed.parent, first)
        self.assertNotEqual(resumed.pk, first.pk)
        self.assertEqual({call["session_id"] for call in complete.calls}, {str(first.pk)})
