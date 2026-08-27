"""Tests for the analytics service: the numbers, the window, and who is allowed to see them.

The class that matters is `TestPermissions`. Asserting that a user with nothing gets nothing is
easy and proves little; asserting that a user constrained to a subset gets *that subset's* numbers
is what fails when somebody aggregates over `objects.all()`.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from nautobot_event_tracker.choices import AgentToolCallStatusChoices, SeverityChoices, TicketStatusChoices
from nautobot_event_tracker.ingestion import config as ingestion_config
from nautobot_event_tracker.models import AgentRun, AgentToolCall, EventTicket, IngestionStats, LLMUsageRecord
from nautobot_event_tracker.services import analytics
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.tests import fixtures


class TestTheSettings(TestCase):
    """The `dashboard` block, and what it refuses."""

    def test_the_defaults(self):
        """On by default, seven days, ninety at most, ten seconds a panel."""
        with fixtures.app_settings():
            settings = analytics.get_settings()

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.default_window_days, 7)
        self.assertEqual(settings.max_window_days, 90)
        self.assertEqual(settings.query_timeout_seconds, 10)

    def test_this_is_the_one_block_that_is_on_by_default(self):
        """Section 4's deliberate inconsistency, asserted so it stays deliberate.

        Every other block reaches a network or spends money on first use. This one runs a handful
        of GROUP BY queries against tables the deployment already has.
        """
        self.assertIs(analytics.DEFAULTS["enabled"], True)

    def test_a_non_boolean_enabled_is_refused(self):
        """A truthy string is not a switch."""
        with fixtures.dashboard_settings(enabled="yes"), self.assertRaises(ImproperlyConfigured) as refused:
            analytics.get_settings()

        self.assertIn("'enabled' must be a boolean", str(refused.exception))

    def test_a_window_that_is_not_a_positive_integer_is_refused(self):
        """`True` is an `int` in Python, which is the half that is easy to leave out."""
        for value in (0, -1, "7", True):
            with self.subTest(value=value):
                with fixtures.dashboard_settings(default_window_days=value), self.assertRaises(ImproperlyConfigured):
                    analytics.get_settings()

    def test_a_default_window_larger_than_the_maximum_is_refused(self):
        """Otherwise the page opens on a window it is not allowed to ask for."""
        with (
            fixtures.dashboard_settings(default_window_days=30, max_window_days=7),
            self.assertRaises(ImproperlyConfigured) as refused,
        ):
            analytics.get_settings()

        self.assertIn("cannot exceed", str(refused.exception))

    def test_a_timeout_that_is_not_positive_is_refused(self):
        """Zero seconds is not a stricter bound, it is a page that renders nothing."""
        for value in (0, -1, "10"):
            with self.subTest(value=value):
                with (
                    fixtures.dashboard_settings(query_timeout_seconds=value),
                    self.assertRaises(ImproperlyConfigured),
                ):
                    analytics.get_settings()

    def test_the_offered_windows_never_exceed_the_maximum(self):
        """Rule D4 at the form: the bound and the offer cannot disagree."""
        with fixtures.dashboard_settings(max_window_days=14, default_window_days=7):
            offered = analytics.window_choices()

        self.assertEqual(offered, (1, 7, 14))


class TestTheSchemaAgreesWithTheValidator(fixtures.SchemaAgreementAssertions, TestCase):
    """The `dashboard` block's half of the contract. The argument is in the shared base."""

    BLOCK = "dashboard"
    SETTINGS_MODULE = analytics
    #: Both window keys are held wide open while either is probed: `default_window_days` may not
    #: exceed `max_window_days`, and that is a rule about two keys, which a per-key schema cannot
    #: express and should not be reported as a disagreement.
    BASE_BLOCK = {"default_window_days": 1, "max_window_days": 3650}
    PROBES = {
        "default_window_days": {"valid": (1, 7), "invalid": (0, -1)},
        "max_window_days": {"valid": (1, 90), "invalid": (0, -1)},
        "query_timeout_seconds": {"valid": (10, 0.5), "invalid": (0, -1)},
    }


class TestTheRetentionDefaultsMatchTheirOwners(TestCase):
    """The two retention numbers this module repeats must equal the ones it may not import.

    `services` imports nothing from `ingestion`, and rule D7 forbids importing `services.llm`, so
    `analytics.py` reads both retention settings out of `PLUGINS_CONFIG` with a default of its own.
    That is a copy, and this is what fails when the original moves.
    """

    def test_the_ingestion_stats_retention_default_matches(self):
        """`StatsRecorder` deletes its own old buckets on this schedule."""
        self.assertEqual(
            analytics.INGESTION_STATS_RETENTION_DEFAULT,
            ingestion_config.DEFAULTS["stats_retention_days"],
        )

    def test_the_llm_usage_retention_default_matches(self):
        """`services.llm` deletes its own old usage records on this one."""
        self.assertEqual(
            analytics.LLM_USAGE_RETENTION_DEFAULT,
            llm_service.DEFAULTS["usage_retention_days"],
        )


class AnalyticsTestCase(TestCase):
    """A superuser, one critical ticket and one minor ticket, and rows hanging off each.

    Built in `setUpTestData` rather than `setUp`: every ticket here goes through the service layer,
    which is a transaction, a dedup lookup and two inserts, and forty test methods rebuilding it
    each time is the bulk of this module's runtime. Django rolls each test back and hands every
    method its own copy of the attributes, so the tests that backdate rows still work.
    """

    @classmethod
    def setUpTestData(cls):
        """Build the corpus every panel test reads, once for the class."""
        cls.user = fixtures.create_user()
        cls.user.is_superuser = True
        cls.user.save()

        cls.model = fixtures.create_llmmodel()
        cls.tool = fixtures.create_mcptool()
        cls.critical = fixtures.create_ticket(user=cls.user, title="core-01 down", severity=SeverityChoices.CRITICAL)
        cls.minor = fixtures.create_ticket(user=cls.user, title="leaf-09 flap", severity=SeverityChoices.MINOR)

    def usage_for(self, ticket, **overrides):
        """One priced model call against this ticket."""
        return fixtures.create_llmusagerecord(model=self.model, ticket=ticket, **overrides)

    def run_for(self, ticket):
        """One agent run against this ticket, with one proposed tool call on it."""
        run = fixtures.create_agentrun(ticket=ticket)
        fixtures.create_agenttoolcall(run=run, tool=self.tool, status=AgentToolCallStatusChoices.APPROVED)
        return run


class TestTheWindow(AnalyticsTestCase):
    """Rule D4: every query is bounded, and retention bounds it further."""

    def test_the_default_window_applies_when_the_page_does_not_ask(self):
        """`days=None` means the configured default, not "all time"."""
        with fixtures.dashboard_settings(default_window_days=3):
            window = analytics.ticket_flow(user=self.user).window

        self.assertEqual(window.days, 3)
        self.assertFalse(window.clamped)

    def test_a_window_over_the_maximum_is_cut_to_the_maximum(self):
        """A bound, not a preference."""
        with fixtures.dashboard_settings(max_window_days=14):
            window = analytics.ticket_flow(user=self.user, days=365).window

        self.assertEqual(window.days, 14)

    def test_rows_outside_the_window_are_not_counted(self):
        """The point of the bound: a ticket opened last year is not this week's news."""
        fixtures.backdate(self.minor, created=timezone.now() - timedelta(days=400))

        with fixtures.dashboard_settings(default_window_days=7):
            opened = analytics.ticket_flow(user=self.user).flow["Opened"]

        self.assertEqual(sum(opened.values()), 1)

    def test_retention_clamps_the_ingestion_window_and_says_so(self):
        """A ninety-day window over a thirty-day retention would draw sixty days of zeroes.

        Those zeroes are deletion, not quiet, and a chart that does not distinguish them is a chart
        that reports an outage nobody had.
        """
        with fixtures.app_settings(
            dashboard={"enabled": True, "max_window_days": 90},
            ingestion={"stats_retention_days": 30},
        ):
            window = analytics.ingestion_health(user=self.user, days=90).window

        self.assertEqual(window.days, 30)
        self.assertEqual(window.requested_days, 90)
        self.assertTrue(window.clamped)
        self.assertIn("retention", window.label)

    def test_retention_clamps_the_cost_window_too(self):
        """`services.llm` prunes its own usage records, so the same reasoning applies to money."""
        with fixtures.app_settings(
            dashboard={"enabled": True, "max_window_days": 90},
            llm={"usage_retention_days": 45},
        ):
            window = analytics.model_cost(user=self.user, days=90).window

        self.assertEqual(window.days, 45)
        self.assertTrue(window.clamped)

    def test_a_window_inside_retention_is_not_clamped(self):
        """The label must not cry retention at a window retention does not touch."""
        with fixtures.app_settings(dashboard={"enabled": True}, ingestion={"stats_retention_days": 30}):
            window = analytics.ingestion_health(user=self.user, days=7).window

        self.assertFalse(window.clamped)
        self.assertEqual(window.label, "last 7 days")

    def test_a_malformed_neighbouring_retention_setting_does_not_take_the_page_down(self):
        """The owning block validates itself and will say so. This one draws the chart."""
        with fixtures.app_settings(dashboard={"enabled": True}, ingestion={"stats_retention_days": "thirty"}):
            window = analytics.ingestion_health(user=self.user, days=7).window

        self.assertEqual(window.days, 7)


class TestIngestionHealth(AnalyticsTestCase):
    """Is the consumer running, and is it doing anything."""

    def test_the_funnel_is_counted_per_day(self):
        """One line per stage, summed across every consumer and topic in the bucket."""
        fixtures.create_ingestionstats(received=10, tickets_opened=3, dropped=7)
        fixtures.create_ingestionstats(topic="other.events", received=5, tickets_opened=1, dropped=4)

        with fixtures.dashboard_settings():
            health = analytics.ingestion_health(user=self.user)

        self.assertEqual(sum(health.volume["Received"].values()), 15)
        self.assertEqual(sum(health.volume["Tickets opened"].values()), 4)
        self.assertEqual(sum(health.volume["Dropped"].values()), 11)

    def test_drops_are_summed_by_reason_across_the_window(self):
        """`drops_by_reason` is a JSONField with operator-supplied keys, so this is done in Python."""
        fixtures.create_ingestionstats(dropped=3, drops_by_reason={"severity_floor": 2, "rate_limit": 1})
        fixtures.create_ingestionstats(topic="other.events", dropped=4, drops_by_reason={"severity_floor": 4})

        with fixtures.dashboard_settings():
            drops = analytics.ingestion_health(user=self.user).drops

        self.assertEqual(dict(zip(drops["x"], drops["series"][0]["data"])), {"severity_floor": 6, "rate_limit": 1})

    def test_the_largest_reason_comes_first(self):
        """A pie whose slices reorder between renders is a pie nobody trusts.

        Handed over in the framework's internal format, because the nested one is re-sorted when
        the x axis is built and this order is the point of the chart.
        """
        fixtures.create_ingestionstats(dropped=5, drops_by_reason={"rate_limit": 1, "severity_floor": 4})

        with fixtures.dashboard_settings():
            drops = analytics.ingestion_health(user=self.user).drops

        self.assertEqual(drops["x"], ["severity_floor", "rate_limit"])

    def test_a_bucket_outside_the_window_is_not_counted(self):
        """Rule D4, on the one table that was already bucketed."""
        old = fixtures.create_ingestionstats(received=99)
        fixtures.backdate(old, bucket_start=timezone.now() - timedelta(days=40))

        with fixtures.dashboard_settings(default_window_days=7):
            health = analytics.ingestion_health(user=self.user)

        self.assertEqual(sum(health.volume["Received"].values()), 0)

    def test_nothing_recorded_draws_nothing(self):
        """An empty deployment gets empty series, not a chart of invented zeroes."""
        with fixtures.dashboard_settings():
            health = analytics.ingestion_health(user=self.user)

        self.assertEqual(health.volume["Received"], {})
        self.assertEqual(health.drops, {})
        self.assertFalse(health.timed_out)


class TestTicketFlow(AnalyticsTestCase):
    """How many tickets, in what state, and how long they take."""

    def test_opened_and_closed_are_counted_per_day(self):
        """The two lines an operator reads together."""
        fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user, title="already done")

        with fixtures.dashboard_settings():
            flow = analytics.ticket_flow(user=self.user).flow

        self.assertEqual(sum(flow["Opened"].values()), 3)
        self.assertEqual(sum(flow["Closed"].values()), 1)

    def test_open_tickets_are_split_by_severity_most_severe_first(self):
        """Ordered by `SEVERITY_WEIGHTS`, so the ordering is not an alphabetical accident."""
        with fixtures.dashboard_settings():
            severities = analytics.ticket_flow(user=self.user).open_by_severity

        self.assertEqual(severities["x"], ["Critical", "Minor"])
        self.assertEqual(severities["series"][0]["data"], [1, 1])

    def test_a_closed_ticket_is_not_an_open_one(self):
        """The severity pie is about work outstanding."""
        fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user, title="done")

        with fixtures.dashboard_settings():
            severities = analytics.ticket_flow(user=self.user).open_by_severity

        self.assertEqual(sum(severities["series"][0]["data"]), 2)

    def test_the_median_time_to_close_is_measured_over_tickets_closed_in_the_window(self):
        """Spec 11.2. Measured by opening date, a long-running incident never appears at all."""
        closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user, title="four hours")
        fixtures.backdate(closed, created=closed.closed_at - timedelta(hours=4))

        with fixtures.dashboard_settings():
            median = analytics.ticket_flow(user=self.user).median_hours_to_close

        self.assertAlmostEqual(median, 4, places=2)

    def test_a_ticket_opened_long_ago_and_closed_this_week_still_counts(self):
        """The reading that does not flatter: the slow ones are the ones worth seeing."""
        closed = fixtures.create_ticket_in_status(TicketStatusChoices.CLOSED, user=self.user, title="slow")
        fixtures.backdate(closed, created=closed.closed_at - timedelta(days=30))

        with fixtures.dashboard_settings(default_window_days=7):
            median = analytics.ticket_flow(user=self.user).median_hours_to_close

        self.assertAlmostEqual(median, 30 * 24, places=1)

    def test_nothing_closed_has_no_median_rather_than_a_zero(self):
        """Zero hours to close is a claim; no median is the truth."""
        with fixtures.dashboard_settings():
            median = analytics.ticket_flow(user=self.user).median_hours_to_close

        self.assertIsNone(median)


class TestModelCost(AnalyticsTestCase):
    """What the models cost, and how often they fail."""

    def test_cost_is_summed_per_day_and_split_by_purpose(self):
        """The chart the LLM registry records prices for."""
        self.usage_for(self.critical)
        self.usage_for(self.minor)

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        self.assertIn("Triage", cost.cost_by_purpose)
        self.assertEqual(len(cost.cost_by_purpose["Triage"]), 1)

    def test_cost_is_read_from_the_record_and_never_recomputed(self):
        """Rule D5. The service priced the call once, against the registry as it stood then.

        A price edited since must not retroactively rewrite what a past call cost, so this asserts
        the chart equals the stored decimals rather than anything derived from token counts.
        """
        first = self.usage_for(self.critical)
        second = self.usage_for(self.minor)

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        charted = sum(sum(series.values()) for series in cost.cost_by_purpose.values())
        self.assertAlmostEqual(charted, float(first.cost + second.cost), places=6)

    def test_cost_is_broken_down_by_model(self):
        """The table beside the chart, so a deployment can see which model is the expensive one."""
        self.usage_for(self.critical)

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        self.assertEqual(list(cost.cost_by_model), [self.model.name])

    def test_the_failure_rate_counts_calls_that_did_not_succeed(self):
        """`LLMUsageRecord` records the failures too, which is why there is a rate to report."""
        self.usage_for(self.critical)
        LLMUsageRecord.objects.filter(pk=self.usage_for(self.minor).pk).update(success=False)

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        self.assertEqual(cost.calls, 2)
        self.assertEqual(cost.failures, 1)
        self.assertAlmostEqual(cost.failure_rate, 0.5)

    def test_spend_under_a_purpose_the_choice_set_no_longer_names_is_still_charted(self):
        """Otherwise the chart under-reports while the figures beside it keep counting.

        The pivot falls back to the raw value for an unrecognized purpose, and those records still
        reach `calls` and the failure rate. Ordering by the declared labels alone silently dropped
        their spend from the chart, which is the one place somebody looks for the total.
        """
        record = self.usage_for(self.critical)
        LLMUsageRecord.objects.filter(pk=record.pk).update(purpose="retired-purpose")

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        charted = sum(sum(series.values()) for series in cost.cost_by_purpose.values())
        self.assertEqual(cost.calls, 1)
        self.assertIn("retired-purpose", cost.cost_by_purpose)
        self.assertAlmostEqual(charted, float(record.cost), places=6)

    def test_declared_purposes_come_before_undeclared_ones(self):
        """A legend that reshuffles between two renders of the same window is a legend nobody reads."""
        self.usage_for(self.critical)
        LLMUsageRecord.objects.filter(pk=self.usage_for(self.minor).pk).update(purpose="retired-purpose")

        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        self.assertEqual(list(cost.cost_by_purpose), ["Triage", "retired-purpose"])

    def test_no_calls_means_no_rate_rather_than_a_zero(self):
        """Nought failures out of nought calls is not a healthy deployment, it is no deployment."""
        with fixtures.dashboard_settings():
            cost = analytics.model_cost(user=self.user)

        self.assertEqual(cost.calls, 0)
        self.assertIsNone(cost.failure_rate)

    def test_a_call_outside_the_window_is_not_counted(self):
        """Rule D4, on the table that grows fastest."""
        record = self.usage_for(self.critical)
        fixtures.backdate(record, called_at=timezone.now() - timedelta(days=40))

        with fixtures.dashboard_settings(default_window_days=7):
            cost = analytics.model_cost(user=self.user)

        self.assertEqual(cost.calls, 0)


class TestAgentActivity(AnalyticsTestCase):
    """Whether the approval gate is used or rubber-stamped."""

    def test_runs_are_counted_per_day_by_status(self):
        """Phase 4B's control, seen from outside itself."""
        self.run_for(self.critical)

        with fixtures.dashboard_settings():
            activity = analytics.agent_activity(user=self.user)

        self.assertEqual(sum(sum(days.values()) for days in activity.runs_by_status.values()), 1)

    def test_the_approve_and_deny_split_is_reported(self):
        """The number the gate cannot report about itself."""
        run = self.run_for(self.critical)
        fixtures.create_agenttoolcall(run=run, tool=self.tool, status=AgentToolCallStatusChoices.DENIED)

        with fixtures.dashboard_settings():
            decisions = analytics.agent_activity(user=self.user).decisions

        counted = dict(zip(decisions["x"], decisions["series"][0]["data"]))
        self.assertEqual(counted["Approved"], 1)
        self.assertEqual(counted["Denied"], 1)
        # Declared order, so approved and denied sit in the same place on every render.
        self.assertEqual(decisions["x"], ["Approved", "Denied"])

    def test_a_run_outside_the_window_is_not_counted(self):
        """Rule D4 on the last of the four tables."""
        run = self.run_for(self.critical)
        fixtures.backdate(run, started_at=timezone.now() - timedelta(days=40))

        with fixtures.dashboard_settings(default_window_days=7):
            activity = analytics.agent_activity(user=self.user)

        self.assertEqual(activity.runs_by_status, {})

    def test_no_agents_draws_nothing(self):
        """A deployment that has never run one gets an empty panel, not a chart of zeroes."""
        with fixtures.dashboard_settings():
            activity = analytics.agent_activity(user=self.user)

        self.assertEqual(activity.runs_by_status, {})
        self.assertEqual(activity.decisions, {})


class DriverError(Exception):
    """Stands in for the psycopg exception Django wraps, which is where the SQLSTATE lives."""

    def __init__(self, pgcode):
        """Carry the SQLSTATE and nothing else."""
        super().__init__(pgcode)
        self.pgcode = pgcode


def _database_error(message, pgcode):
    """A Django `OperationalError` wrapping a driver error with this SQLSTATE, as psycopg raises."""
    error = OperationalError(message)
    error.__cause__ = DriverError(pgcode)
    return error


class TestTheDeadline(AnalyticsTestCase):
    """Rule D8: a slow panel says so, and a broken database does not pretend to be one."""

    def test_a_cancelled_statement_becomes_a_timed_out_panel(self):
        """The case the timeout exists for."""
        cancelled = _database_error("canceling statement due to statement timeout", analytics.QUERY_CANCELED)

        with fixtures.dashboard_settings(), mock.patch.object(analytics, "visible_stats", side_effect=cancelled):
            health = analytics.ingestion_health(user=self.user)

        self.assertTrue(health.timed_out)
        self.assertEqual(health.volume, {})

    def test_a_database_that_has_gone_away_is_not_reported_as_slowness(self):
        """A failover raises `OperationalError` too, and "try a shorter window" will never fix it.

        Swallowing it would put a real outage behind a panel apologising for being slow, and send
        whoever is reading `docs/admin/dashboard.md` off to check indexes.
        """
        gone = _database_error("server closed the connection unexpectedly", "08006")

        with fixtures.dashboard_settings(), mock.patch.object(analytics, "visible_stats", side_effect=gone):
            with self.assertRaises(OperationalError):
                analytics.ingestion_health(user=self.user)


class TestPermissions(AnalyticsTestCase):
    """Rule D2 - the thing this phase must not get wrong.

    An aggregate leaks without returning anything. A user constrained to one subset of tickets,
    shown a count of the whole estate, has learned its size; a severity pie tells them its shape;
    a cost chart tells them what the deployment spends. None of those responses contains one record
    they were forbidden to read, and every one is a disclosure.

    Phase 5A wrote R6 about a panel and left the REST viewset, the list view and the GraphQL type
    unguarded. These tests are written about the module, per function, for that reason.
    """

    #: Every public function that reads. `get_settings` and `window_choices` touch no queryset.
    PANELS = ("ingestion_health", "ticket_flow", "model_cost", "agent_activity")

    def setUp(self):
        """Add a second, unprivileged user and give every table something to count."""
        super().setUp()
        fixtures.create_ingestionstats(received=10, tickets_opened=3, dropped=7)
        self.usage_for(self.critical)
        self.usage_for(self.minor)
        self.run_for(self.critical)
        self.run_for(self.minor)

        self.nobody = fixtures.create_user(username="nobody")

    def _constrained_user(self):
        """A user who may read every record model, but only the critical ticket.

        The permissions on the record models are deliberately unconstrained. That is what makes the
        test meaningful: if the numbers still narrow, they narrowed because the restriction
        followed the parent, and not because the record model happened to be locked down too.
        """
        user = fixtures.create_user(username="constrained")
        fixtures.grant_view(user, EventTicket, constraints={"severity": SeverityChoices.CRITICAL})
        for model in (IngestionStats, LLMUsageRecord, AgentRun, AgentToolCall):
            fixtures.grant_view(user, model)
        return user

    def test_a_user_with_no_permissions_gets_zeroes(self):
        """The easy half, asserted for all four so none is forgotten."""
        with fixtures.dashboard_settings():
            for name in self.PANELS:
                with self.subTest(panel=name):
                    result = getattr(analytics, name)(user=self.nobody)
                    self.assertEqual(_totals(result), 0)

    def test_an_anonymous_user_gets_zeroes(self):
        """A dashboard reached without logging in counts nothing."""
        with fixtures.dashboard_settings():
            for name in self.PANELS:
                with self.subTest(panel=name):
                    result = getattr(analytics, name)(user=AnonymousUser())
                    self.assertEqual(_totals(result), 0)

    def test_a_constrained_user_sees_their_own_ticket_numbers(self):
        """Not the deployment's. This is the assertion that fails on `objects.all()`."""
        user = self._constrained_user()

        with fixtures.dashboard_settings():
            flow = analytics.ticket_flow(user=user)

        self.assertEqual(sum(flow.flow["Opened"].values()), 1)
        self.assertEqual(flow.open_by_severity["x"], ["Critical"])

    def test_a_constrained_user_sees_only_their_own_tickets_costs(self):
        """A cost chart built from unrestricted usage records is a ticket-existence oracle."""
        user = self._constrained_user()

        with fixtures.dashboard_settings():
            unconstrained = analytics.model_cost(user=self.user)
            constrained = analytics.model_cost(user=user)

        self.assertEqual(unconstrained.calls, 2)
        self.assertEqual(constrained.calls, 1)

    def test_a_call_naming_no_ticket_is_still_counted(self):
        """`LLMUsageRecord.ticket` is nullable, and a call that names nothing discloses nothing.

        Excluding these would understate the deployment's real spend for every user while
        protecting nothing: there is no ticket for the row to be an oracle about.
        """
        self.usage_for(None)
        user = self._constrained_user()

        with fixtures.dashboard_settings():
            constrained = analytics.model_cost(user=user)

        self.assertEqual(constrained.calls, 2)

    def test_a_constrained_user_sees_only_their_own_tickets_agent_runs(self):
        """`AgentRun.ticket` is not nullable, so every run follows its parent."""
        user = self._constrained_user()

        with fixtures.dashboard_settings():
            unconstrained = analytics.agent_activity(user=self.user)
            constrained = analytics.agent_activity(user=user)

        self.assertEqual(_run_total(unconstrained), 2)
        self.assertEqual(_run_total(constrained), 1)

    def test_a_constrained_user_sees_only_their_own_tickets_tool_calls(self):
        """The approve/deny split reaches its ticket through the run."""
        user = self._constrained_user()

        with fixtures.dashboard_settings():
            decisions = analytics.agent_activity(user=user).decisions

        self.assertEqual(sum(decisions["series"][0]["data"]), 1)

    def test_ingestion_counters_need_their_own_permission(self):
        """The one model here with no parent: a counter names a consumer and a topic, not a ticket."""
        user = fixtures.create_user(username="ticketsonly")
        fixtures.grant_view(user, EventTicket)

        with fixtures.dashboard_settings():
            health = analytics.ingestion_health(user=user)

        self.assertEqual(sum(health.volume["Received"].values()), 0)


def _totals(result):
    """Every number a panel result carries, added together. Zero means it disclosed nothing.

    Reads both chart shapes: the nested one the bar and line charts use, and the framework's
    internal one the pies are handed over in.
    """
    total = 0
    for mapping in (
        getattr(result, "volume", {}),
        getattr(result, "drops", {}),
        getattr(result, "flow", {}),
        getattr(result, "open_by_severity", {}),
        getattr(result, "cost_by_purpose", {}),
        getattr(result, "runs_by_status", {}),
        getattr(result, "decisions", {}),
    ):
        if "series" in mapping:
            total += sum(sum(series["data"]) for series in mapping["series"])
            continue
        for series in mapping.values():
            total += sum(series.values())
    total += sum(getattr(result, "cost_by_model", {}).values())
    total += getattr(result, "calls", 0)
    return total


def _run_total(activity):
    """How many agent runs an activity result counted, across every status and day."""
    return sum(sum(days.values()) for days in activity.runs_by_status.values())
