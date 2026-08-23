"""The agent Job: its declaration, and what it reports about a run it started.

The Job holds nothing but a Job's own concerns, so these tests are about exactly that - the input,
the log lines, and whether the JobResult ends up the right colour. What the agent *does* is tested
in `test_services_agent.py`, where the loop lives.
"""

from unittest import mock

from nautobot.apps.choices import JobResultStatusChoices
from nautobot.apps.testing import TestCase, TransactionTestCase, create_job_result_and_run_job
from nautobot.extras.models import Job

from nautobot_event_tracker.choices import AgentRunStatusChoices, TicketStatusChoices
from nautobot_event_tracker.jobs import EventTicketAgentJob
from nautobot_event_tracker.models import AgentRun, AgentToolCall
from nautobot_event_tracker.tests import fixtures

MODULE = "nautobot_event_tracker.jobs"
JOB_NAME = "EventTicketAgentJob"


class TestTheJobDeclaration(TestCase):
    """Section 6.3: the four attributes that are load-bearing, and one that would be silent."""

    def test_the_job_is_registered(self):
        """A Job nobody registered is a Job nobody can run."""
        self.assertTrue(Job.objects.filter(module_name=MODULE, job_class_name=JOB_NAME).exists())

    def test_a_ticket_id_is_not_a_credential(self):
        """`has_sensitive_variables` defaults to True, and blocks scheduling and approval workflows."""
        self.assertFalse(EventTicketAgentJob.has_sensitive_variables)

    def test_the_hard_limit_is_above_the_soft_one(self):
        """Otherwise Nautobot kills the Job without the Job ever finding out, which is A6's fear."""
        self.assertGreater(EventTicketAgentJob.time_limit, EventTicketAgentJob.soft_time_limit)

    def test_its_only_input_is_a_ticket(self):
        """A9 handles "one at a time"; the Job does not need a second variable to say so."""
        self.assertEqual(list(EventTicketAgentJob._get_vars()), ["ticket"])  # pylint: disable=protected-access


class JobRunTestCase(TransactionTestCase):
    """A ticket, a model, and the settings that let a run start.

    A `TransactionTestCase` rather than the usual `TestCase`, because a Job writes its log entries
    to a separate database alias and Django's test isolation will not let a transaction-wrapped
    test reach one. Slower, and the alternative is running the Job with its logging broken.
    """

    databases = ("default", "job_logs")

    def setUp(self):
        """Create the ticket the Job is pointed at."""
        super().setUp()
        fixtures.create_event_types()
        self.ticket = fixtures.create_ticket(user=self.user)
        fixtures.create_llmmodel()

    def run_job(self, complete=None, call_tool=None):
        """Run the Job for real, with the agent's two seams standing in for the world."""
        real_run_agent = _real_run_agent()
        with fixtures.agent_settings():
            with mock.patch(
                "nautobot_event_tracker.services.agent.run_agent",
                side_effect=lambda **kwargs: real_run_agent(
                    **kwargs,
                    complete=complete if complete is not None else fixtures.FakeAgentComplete("Nothing to report."),
                    call_tool=call_tool if call_tool is not None else fixtures.FakeToolCaller(),
                ),
            ):
                return create_job_result_and_run_job(MODULE, JOB_NAME, job_kwargs={"ticket": str(self.ticket.pk)})


class TestRunningTheJob(JobRunTestCase):
    """What the Job does with what the service hands back."""

    def test_a_completed_run_is_a_successful_job(self):
        """The ordinary case, end to end through Nautobot's own runner."""
        job_result = self.run_job()

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_SUCCESS)
        run = AgentRun.objects.get()
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn(str(run.pk), job_result.result)

    def test_a_run_that_stopped_at_the_gate_is_a_successful_job(self):
        """It did what it was asked and ended; the thing waiting is a person, not the Job."""
        fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)

        job_result = self.run_job(
            complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        )

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_SUCCESS)
        self.assertEqual(AgentRun.objects.get().status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual(AgentToolCall.objects.get().tool.name, "push_config")

    def test_the_proposal_is_named_in_the_job_s_log(self):
        """A run that ends waiting is not a run that ended; the log has to say which it was."""
        fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)

        job_result = self.run_job(
            complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        )

        logs = " ".join(job_result.job_log_entries.values_list("message", flat=True))
        self.assertIn("push_config", logs)
        self.assertIn("Nothing has been called", logs)

    def test_a_failed_run_fails_the_job(self):
        """A6 - a run that failed and reported success is the failure this rule is named for."""
        from nautobot_event_tracker.services.exceptions import LLMCallError  # pylint: disable=C0415

        def _failing(**_kwargs):
            raise LLMCallError("the provider refused")

        job_result = self.run_job(complete=_failing)

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_FAILURE)
        self.assertEqual(AgentRun.objects.get().status, AgentRunStatusChoices.FAILED)

    def test_the_run_is_linked_to_its_job_result(self):
        """Two records of one run, each pointing at the other's half of the story."""
        job_result = self.run_job()

        self.assertEqual(AgentRun.objects.get().job_result_id, job_result.pk)

    def test_a_refusal_is_a_failed_job_rather_than_a_traceback(self):
        """Agents switched off is a sentence an operator can act on."""
        job_result = create_job_result_and_run_job(MODULE, JOB_NAME, job_kwargs={"ticket": str(self.ticket.pk)})

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_FAILURE)
        self.assertFalse(AgentRun.objects.exists())

    def test_a_second_run_on_a_busy_ticket_is_refused(self):
        """A9, as the Job reports it."""
        fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.RUNNING)

        job_result = self.run_job()

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_FAILURE)
        self.assertEqual(AgentRun.objects.count(), 1)

    def test_a_resolved_ticket_is_refused(self):
        """S3 - and the refusal costs no model call."""
        self.ticket = fixtures.create_ticket_in_status(TicketStatusChoices.RESOLVED, user=self.user)

        job_result = self.run_job()

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_FAILURE)
        self.assertFalse(AgentToolCall.objects.exists())


def _real_run_agent():
    """The real `run_agent`, captured before anything patches the name it lives under."""
    from nautobot_event_tracker.services import agent as agent_service  # pylint: disable=C0415

    return agent_service.run_agent
