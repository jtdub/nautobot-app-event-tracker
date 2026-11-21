"""The agent Job: its declaration, and what it reports about a run it started.

The Job holds nothing but a Job's own concerns, so these tests are about exactly that - the input,
the log lines, and whether the JobResult ends up the right colour. What the agent *does* is tested
in `test_services_agent.py`, where the loop lives.
"""

from types import SimpleNamespace
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
        self.run_agent_calls = []
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
            ) as patched:
                job_result = create_job_result_and_run_job(MODULE, JOB_NAME, job_kwargs={"ticket": str(self.ticket.pk)})
        self.run_agent_calls = patched.call_args_list
        return job_result

    @staticmethod
    def set_job_limits(*, soft, hard):
        """Override the two time limits on the Job row, as the Job edit page does."""
        Job.objects.filter(module_name=MODULE, job_class_name=JOB_NAME).update(
            soft_time_limit=soft, soft_time_limit_override=True, time_limit=hard, time_limit_override=True
        )

    @staticmethod
    def log_text(job_result):
        """Every line of the Job log, joined."""
        return " ".join(job_result.job_log_entries.values_list("message", flat=True))


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
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

        job_result = self.run_job(
            complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("push_config", {"device": "leaf-01"})])
        )

        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_SUCCESS)
        self.assertEqual(AgentRun.objects.get().status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertEqual(AgentToolCall.objects.get().tool.name, "push_config")

    def test_the_proposal_is_named_in_the_job_s_log(self):
        """A run that ends waiting is not a run that ended; the log has to say which it was."""
        fixtures.create_mcptool(name="push_config", enabled=True, writable=True)

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

    def test_the_job_passes_the_limits_of_its_row_to_the_service(self):
        """AC-4 - a stock Job passes 600 and 660, and a normal run has no warning about its first call."""
        job_result = self.run_job()

        self.assertEqual(self.run_agent_calls[0].kwargs["soft_time_limit"], 600)
        self.assertEqual(self.run_agent_calls[0].kwargs["time_limit"], 660)
        self.assertNotIn("before its first model call", self.log_text(job_result))

    def test_a_lower_limit_on_the_job_row_stops_the_run_at_that_limit(self):
        """AC-4 - an operator who lowers the limit on the Job edit page gets a clean stop at the new number."""
        self.set_job_limits(soft=80, hard=120)

        job_result = self.run_job()

        self.assertEqual(self.run_agent_calls[0].kwargs["soft_time_limit"], 80)
        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_SUCCESS)
        self.assertEqual(AgentRun.objects.get().status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("of 80 seconds used", self.ticket.updates.latest("created").message)

    def test_a_run_with_no_model_call_writes_a_warning_to_the_job_log(self):
        """AC-8 - a run that ended before its first call must not look like a normal success."""
        self.set_job_limits(soft=80, hard=120)

        job_result = self.run_job()

        self.assertIn(
            "The run ended before its first model call. A limit leaves no room for one call. "
            "Read the last comment on the ticket.",
            self.log_text(job_result),
        )


class TestTheAppliedTimeLimits(TestCase):
    """AC-4 - the Job reads the limits that Celery applied, then the row, then the settings."""

    @staticmethod
    def job_with(*, timelimit, row_soft=600.0, row_hard=660.0):
        """A Job object with a stand-in request and a stand-in row."""
        job = EventTicketAgentJob()
        job.request = SimpleNamespace(timelimit=timelimit)
        job.__dict__["job_model"] = SimpleNamespace(soft_time_limit=row_soft, time_limit=row_hard)
        return job

    def test_the_limits_of_the_request_win(self):
        """Celery keeps the header as `(hard, soft)`, and the Job returns `(soft, hard)`."""
        job = self.job_with(timelimit=(260, 200))

        self.assertEqual(job._applied_time_limits(), (200, 260))  # pylint: disable=protected-access

    def test_a_request_with_no_limits_falls_back_to_the_row(self):
        """A run from the Job page in a test, or a worker that sent none."""
        job = self.job_with(timelimit=(None, None), row_soft=90.0, row_hard=95.0)

        self.assertEqual(job._applied_time_limits(), (90.0, 95.0))  # pylint: disable=protected-access

    def test_a_row_limit_of_zero_falls_back_to_the_settings(self):
        """Nautobot then sends no limit, and the worker uses its own default."""
        job = self.job_with(timelimit=None, row_soft=0.0, row_hard=0.0)

        with self.settings(CELERY_TASK_SOFT_TIME_LIMIT=300, CELERY_TASK_TIME_LIMIT=600):
            self.assertEqual(job._applied_time_limits(), (300, 600))  # pylint: disable=protected-access


def _real_run_agent():
    """The real `run_agent`, captured before anything patches the name it lives under."""
    from nautobot_event_tracker.services import agent as agent_service  # pylint: disable=C0415

    return agent_service.run_agent


class TestTheIndexingJobHook(TestCase):
    """The receiver that puts a closed ticket into the corpus.

    Tested by calling `receive_job_hook` directly rather than through Nautobot's Job Hook
    machinery: what is worth asserting is the decision it makes about which changes are its
    business, and that a failure never escapes.
    """

    def setUp(self):
        """A closed ticket and an embedding model."""
        super().setUp()
        fixtures.create_event_types()
        fixtures.create_embedding_model()
        self.closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user)

    @staticmethod
    def receiver():
        """The receiver, constructed the way a Job is."""
        from nautobot_event_tracker.jobs import IndexClosedTicket  # pylint: disable=C0415

        return IndexClosedTicket()

    def test_a_closed_ticket_is_indexed(self):
        """The path that matters: a person closes a ticket and it joins the corpus."""
        from nautobot_event_tracker.models import TicketEmbedding  # pylint: disable=C0415

        with fixtures.rag_settings():
            with mock.patch(
                "nautobot_event_tracker.services.rag.llm_service.embed",
                side_effect=_real_embed_with([1.0, 0.0, 0.0]),
            ):
                self.receiver().receive_job_hook(change=None, action="update", changed_object=self.closed)

        self.assertTrue(TicketEmbedding.objects.filter(ticket=self.closed).exists())

    def test_an_open_ticket_is_not_its_business(self):
        """It fires on every change to every ticket, so deciding that first is most of the job."""
        from nautobot_event_tracker.models import TicketEmbedding  # pylint: disable=C0415

        open_ticket = fixtures.create_ticket(user=self.user, title="still open")

        with fixtures.rag_settings():
            self.receiver().receive_job_hook(change=None, action="update", changed_object=open_ticket)

        self.assertFalse(TicketEmbedding.objects.exists())

    def test_a_failure_never_escapes(self):
        """R5 - a close is a person finishing work, and must not fail because a model is down."""
        from nautobot_event_tracker.services.exceptions import LLMCallError  # pylint: disable=C0415

        def _fail(**_kwargs):
            raise LLMCallError("the provider refused")

        with fixtures.rag_settings():
            with mock.patch("nautobot_event_tracker.services.rag.llm_service.embed", side_effect=_fail):
                # No assertion needed beyond this not raising: that is the whole rule.
                self.receiver().receive_job_hook(change=None, action="update", changed_object=self.closed)


def _real_embed_with(vector):
    """A side effect that calls the real `embed` with a fake client.

    The real function is captured before the patch is applied: inside the side effect
    `llm_service.embed` is the mock, and calling it would recurse.
    """
    from nautobot_event_tracker.services import llm as llm_service  # pylint: disable=C0415

    real_embed = llm_service.embed

    def _embed(**kwargs):
        return real_embed(**kwargs, client=fixtures.FakeEmbeddingClient(vector))

    return _embed
