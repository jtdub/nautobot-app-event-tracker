"""The analytics dashboard's queries: aggregation, and nothing else.

Four phases of this app have been recording. `IngestionStats` has bucketed the ingestion funnel
since Phase 2, `LLMUsageRecord` has priced every model call since Phase 3, and `AgentRun` has
recorded every investigation since Phase 4B. This module reads those rows and answers the three
questions an operator actually asks - is ingestion healthy, what is this costing, is any of it
working - without anybody writing a query by hand.

Rules implemented here, referenced by number from the Phase 5B spec:

* **D1** - the dashboard writes nothing. No model, no migration, no counter, no cache row. A guard
  asserts this module contains no write call at all.
* **D2** - every aggregate is computed over a queryset restricted to the requesting user. A count
  is an oracle: it answers "how many exist" without returning one of them. Every public function
  here takes `user` as a keyword argument with no default, because the rule is only enforceable if
  there is no way to call one without saying who is asking.
* **D4** - every query is bounded by an explicit time window, and the window is bounded by
  `max_window_days`. There is no "all time".
* **D5** - money comes from `LLMUsageRecord.cost` and is never recomputed from token counts. The
  service layer priced it once, at call time, against the registry as it stood then (rule L5).
* **D6** - every query in this phase lives here. The view assembles panels and holds no ORM.
* **D7** - no LLM call. The dashboard shows numbers a person reads; it does not summarize them
  with a model. A guard asserts this module imports nothing from `services.llm`, `services.agent`
  or `services.rag`.
* **D8** - slow is a bug, not a tuning problem. Each panel's queries run under a statement timeout
  and a panel that exceeds it reports that rather than failing the page.

Two consequences of D7 and of the service layer's standing rule that it imports nothing from
`ingestion` are visible below: this module reads the `ingestion` and `llm` retention settings out
of `PLUGINS_CONFIG` directly, and it repeats the shape of `services.rag.visible_embeddings` rather
than importing it. Both duplications are the price of the import direction, and both are covered by
tests that fail if the copies drift apart.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connection, transaction
from django.db.models import Aggregate, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from nautobot.apps.utils import deepmerge

from nautobot_event_tracker.choices import (
    SEVERITY_WEIGHTS,
    TERMINAL_STATUSES,
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMPurposeChoices,
    SeverityChoices,
)
from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    EventTicket,
    IngestionStats,
    LLMUsageRecord,
)

#: Defaults for the `dashboard` block, applied per key here rather than left to Nautobot's
#: top-level `PLUGINS_CONFIG` merge, for the reason `ingestion.config` documents.
#:
#: `enabled` is `True` where every other block in this app defaults to `False`. The difference is
#: that the others reach a network or spend money on first use, and this one runs a handful of
#: GROUP BY queries against tables the deployment already has.
DEFAULTS = {
    "enabled": True,
    "default_window_days": 7,
    "max_window_days": 90,
    "query_timeout_seconds": 10,
}

#: Retention defaults belonging to other blocks, repeated here because this module may not import
#: their owners: `services` imports nothing from `ingestion`, and rule D7 forbids importing
#: `services.llm`. `tests/test_services_analytics.py` asserts these still match the values in
#: `ingestion/config.py` and `services/llm.py`, so a change in either owner fails the build here.
INGESTION_STATS_RETENTION_DEFAULT = 30
LLM_USAGE_RETENTION_DEFAULT = 90


class Median(Aggregate):  # pylint: disable=abstract-method
    """The median of an expression, as PostgreSQL's ordered-set aggregate.

    In the database rather than in Python because the alternative is dragging every closed
    ticket's timestamps into the process to sort them, which is exactly the forty-fast-queries
    failure mode rule D8 exists to prevent. PostgreSQL only, which ADR 0003 already commits to.
    """

    function = "PERCENTILE_CONT"
    name = "median"
    template = "%(function)s(0.5) WITHIN GROUP (ORDER BY %(expressions)s)"


@dataclass(frozen=True)
class DashboardSettings:
    """The `dashboard` block, parsed and checked."""

    enabled: bool
    default_window_days: int
    max_window_days: int
    query_timeout_seconds: float


@dataclass(frozen=True)
class Window:
    """The time range one panel actually covered, which is not always the one that was asked for.

    `IngestionStats` and `LLMUsageRecord` are pruned by the processes that write them, so a
    ninety-day window over a thirty-day retention would draw sixty days of zeroes and present
    deletion as quiet. `days` is what the panel got; `requested_days` is what the page asked for;
    `label` says which, so no chart can silently invent a number.
    """

    start: object
    end: object
    days: int
    requested_days: int

    @property
    def clamped(self):
        """Whether retention shortened this window."""
        return self.days < self.requested_days

    @property
    def label(self):
        """The window as a phrase for a panel header."""
        if self.clamped:
            return f"last {self.days} days (retention; {self.requested_days} requested)"
        return f"last {self.days} days"


@dataclass(frozen=True)
class IngestionHealth:
    """Is the consumer running, and is it doing anything."""

    window: Window
    volume: dict
    drops: dict
    timed_out: bool = False


@dataclass(frozen=True)
class TicketFlow:
    """How many tickets, in what state, and how long they take."""

    window: Window
    flow: dict
    open_by_severity: dict
    median_hours_to_close: object = None
    timed_out: bool = False


@dataclass(frozen=True)
class ModelCost:
    """What the models cost, by day, by purpose and by model. USD, per `LLMUsageRecord.cost`."""

    window: Window
    cost_by_purpose: dict
    cost_by_model: dict
    calls: int = 0
    failures: int = 0
    timed_out: bool = False

    @property
    def failure_rate(self):
        """Failed calls as a fraction of all calls in the window, or None when there were none."""
        if not self.calls:
            return None
        return self.failures / self.calls


@dataclass(frozen=True)
class AgentActivity:
    """How often agents run, and whether the approval gate is used or rubber-stamped."""

    window: Window
    runs_by_status: dict
    decisions: dict
    timed_out: bool = False


class _QueryTooSlow(Exception):
    """One panel's queries exceeded `query_timeout_seconds`. Private: no caller outside sees it."""


def get_settings():
    """The `dashboard` block with defaults applied per key, refusing a value that cannot work.

    Raises `ImproperlyConfigured`, as every other block's reader does: a settings fault does not
    repair itself between two page loads, and it is not "the chart was empty".
    """
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get("dashboard") or {}
    merged = deepmerge(DEFAULTS, configured)

    problems = []
    if not isinstance(merged["enabled"], bool):
        problems.append(f"'enabled' must be a boolean, got {merged['enabled']!r}")
    for key in ("default_window_days", "max_window_days"):
        value = merged[key]
        # The `bool` clause is the half that is easy to leave out: in Python `True` is an `int`.
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"'{key}' must be a positive integer, got {value!r}")
    timeout = merged["query_timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        problems.append(f"'query_timeout_seconds' must be a positive number, got {timeout!r}")
    if not problems and merged["default_window_days"] > merged["max_window_days"]:
        # Checked only once both are known to be integers, so the message says the useful thing
        # rather than comparing a string with a number.
        problems.append(
            f"'default_window_days' ({merged['default_window_days']}) cannot exceed "
            f"'max_window_days' ({merged['max_window_days']})"
        )

    if problems:
        raise ImproperlyConfigured("nautobot_event_tracker: dashboard " + "; ".join(problems))

    return DashboardSettings(
        enabled=merged["enabled"],
        default_window_days=merged["default_window_days"],
        max_window_days=merged["max_window_days"],
        query_timeout_seconds=float(merged["query_timeout_seconds"]),
    )


def is_enabled():
    """Whether the dashboard exists, read without validating the rest of the block.

    `urls.py` and `navigation.py` consult this at import time, and they must not raise there. A
    typo in `query_timeout_seconds` should break the dashboard page, where somebody can read the
    message; raising in the URLConf would instead stop Nautobot serving anything at all. That is
    the posture `_retention_days` takes about a neighbouring block, applied to this one's own keys.

    `get_settings()` stays strict and runs where the page can report it - the view and the form.
    """
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get("dashboard") or {}
    enabled = configured.get("enabled", DEFAULTS["enabled"])
    return enabled if isinstance(enabled, bool) else DEFAULTS["enabled"]


def window_choices(settings=None):
    """The windows the page offers, bounded by `max_window_days` - rule D4 at the form.

    Returned rather than written into `forms.py` so that the bound and the offer cannot disagree.
    """
    settings = settings or get_settings()
    # `get_settings()` has already refused a default larger than the maximum, so both bounds are
    # safe to add unconditionally.
    offered = {days for days in (1, 7, 14, 30, 60, 90) if days <= settings.max_window_days}
    return tuple(sorted(offered | {settings.max_window_days, settings.default_window_days}))


def _retention_days(block, key, default):
    """One retention setting, read raw because this module may not import the block's owner.

    A value this module cannot use - a string, a boolean, a negative number - is treated as "no
    clamp" rather than raised on. The owning module validates its own block and will say so; the
    dashboard's job when a neighbouring setting is malformed is to draw the chart, not to take the
    page down over somebody else's key.
    """
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get(block) or {}
    value = configured.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _window(days, settings, *, retention_days=None):
    """The bounded, retention-clamped range every query runs over - rule D4.

    `days` of None means the page did not ask, so the configured default applies.
    """
    requested = settings.default_window_days if days is None else days
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        requested = settings.default_window_days
    requested = min(requested, settings.max_window_days)

    effective = requested if retention_days is None else min(requested, retention_days)
    end = timezone.now()
    return Window(start=end - timedelta(days=effective), end=end, days=effective, requested_days=requested)


@contextmanager
def _deadline(seconds):
    """Bound one panel's queries with PostgreSQL's `statement_timeout` - rule D8.

    `SET LOCAL` lasts until the end of the transaction rather than the end of a savepoint, so under
    an *enclosing* transaction - Django's `TestCase` wraps a whole test in one - the setting would
    outlive this block and impose one panel's timeout on every query after it. The reset in the
    `finally` is what stops that, and it is guarded on `in_atomic_block` because outside such a
    transaction there is nothing left to reset: the commit above already discarded it, and
    PostgreSQL answers a bare `SET LOCAL` with a warning and no effect.

    Raises `_QueryTooSlow` so the caller returns the empty result for its panel and the page still
    renders. A panel that says it took too long is a defect somebody can see; a page that never
    returns is one they can only guess at.
    """
    milliseconds = max(1, int(seconds * 1000))
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = %s", [milliseconds])
            yield
    except OperationalError as error:
        raise _QueryTooSlow() from error
    finally:
        if connection.in_atomic_block:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = DEFAULT")


def visible_tickets(user):
    """Tickets `user` may view. Every other restriction here is derived from this one - rule D2."""
    return EventTicket.objects.restrict(user, "view")


def visible_stats(user):
    """Ingestion counters `user` may view.

    The only model in this module with no parent to follow: a counter row names a consumer and a
    topic, not a ticket, so `view_ingestionstats` is the whole of the question.
    """
    return IngestionStats.objects.restrict(user, "view")


def visible_usage(user):
    """Usage records `user` may view, following the ticket when there is one - rule D2.

    Gated on `view_llmusagerecord` alone, a cost chart is a ticket-existence oracle with a price
    attached: a record names its ticket, so a sum over unrestricted records tells somebody scoped
    out of an estate how large that estate is and what it costs to run.

    Records naming no ticket are included. `LLMUsageRecord.ticket` is nullable, and a call made
    outside any ticket - a triage decision that refused to open one - names nothing, so it
    discloses nothing about which tickets exist. Excluding them would understate the deployment's
    real spend for every user without protecting anything.

    Shaped after `services.rag.visible_embeddings`, deliberately not imported: rule D7 forbids this
    module importing `services.rag`, and duplicated shape is the price of that direction.
    """
    return LLMUsageRecord.objects.restrict(user, "view").filter(
        Q(ticket__isnull=True) | Q(ticket__in=visible_tickets(user).values("pk"))
    )


def visible_runs(user):
    """Agent runs `user` may view, following the ticket - rule D2.

    `AgentRun.ticket` is not nullable, so unlike usage records there is no unattached case.
    """
    return AgentRun.objects.restrict(user, "view").filter(ticket__in=visible_tickets(user).values("pk"))


def visible_tool_calls(user):
    """Proposed tool calls `user` may view, following the run's ticket - rule D2."""
    return AgentToolCall.objects.restrict(user, "view").filter(run__ticket__in=visible_tickets(user).values("pk"))


def _by_day(rows, value_key):
    """Turn dated aggregate rows into the nested dict `EChartsPanel` reads.

    ISO dates sort as strings the way they sort as dates, which is what the framework's own
    transform relies on when it builds the x axis.
    """
    return {str(row["day"]): row[value_key] for row in rows if row["day"] is not None}


def ingestion_health(*, user, days=None):
    """The ingestion funnel over the window: what arrived, what opened a ticket, what was dropped.

    Clamped to `ingestion.stats_retention_days`, because `StatsRecorder` deletes its own old
    buckets and a window past that point would draw zeroes it invented.
    """
    settings = get_settings()
    window = _window(
        days,
        settings,
        retention_days=_retention_days("ingestion", "stats_retention_days", INGESTION_STATS_RETENTION_DEFAULT),
    )
    try:
        with _deadline(settings.query_timeout_seconds):
            stats = visible_stats(user).filter(bucket_start__gte=window.start, bucket_start__lte=window.end)
            rows = list(
                stats.annotate(day=TruncDate("bucket_start"))
                .values("day")
                .annotate(
                    received=Sum("received"),
                    tickets_opened=Sum("tickets_opened"),
                    dropped=Sum("dropped"),
                )
                .order_by("day")
            )

            # `drops_by_reason` is a JSONField with operator-supplied keys, so there is no clean
            # SQL aggregate for it. A second statement rather than one pass carrying the column
            # beside the counters: `exclude(drops_by_reason={})` means a deployment dropping
            # nothing reads no rows at all, where a merged query would pull every bucket in the
            # window into Python to sum counts the database has already summed.
            reasons = {}
            for mapping in stats.exclude(drops_by_reason={}).values_list("drops_by_reason", flat=True):
                for reason, count in (mapping or {}).items():
                    if isinstance(count, int) and not isinstance(count, bool):
                        reasons[reason] = reasons.get(reason, 0) + count
    except _QueryTooSlow:
        return IngestionHealth(window=window, volume={}, drops={}, timed_out=True)

    volume = {
        "Received": _by_day(rows, "received"),
        "Tickets opened": _by_day(rows, "tickets_opened"),
        "Dropped": _by_day(rows, "dropped"),
    }
    drops = {"Dropped": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))} if reasons else {}
    return IngestionHealth(window=window, volume=volume, drops=drops)


def ticket_flow(*, user, days=None):
    """Tickets opened and closed per day, open tickets by severity, and median time to close.

    Time to close is measured over tickets *closed* within the window rather than opened within it
    (spec 11.2). The two readings differ most exactly when things are going badly: measured by
    opening date, a long-running incident never appears at all, and the number flatters.

    No retention clamp: nothing prunes `EventTicket`.
    """
    settings = get_settings()
    window = _window(days, settings)
    try:
        with _deadline(settings.query_timeout_seconds):
            tickets = visible_tickets(user)
            opened = list(
                tickets.filter(created__gte=window.start, created__lte=window.end)
                .annotate(day=TruncDate("created"))
                .values("day")
                .annotate(total=Count("pk"))
                .order_by("day")
            )
            closed = list(
                tickets.filter(closed_at__gte=window.start, closed_at__lte=window.end)
                .annotate(day=TruncDate("closed_at"))
                .values("day")
                .annotate(total=Count("pk"))
                .order_by("day")
            )
            severities = list(
                tickets.exclude(status__in=TERMINAL_STATUSES).values("severity").annotate(total=Count("pk"))
            )
            elapsed = ExpressionWrapper(F("closed_at") - F("created"), output_field=DurationField())
            median = tickets.filter(
                closed_at__gte=window.start,
                closed_at__lte=window.end,
                created__isnull=False,
            ).aggregate(median=Median(elapsed, output_field=DurationField()))["median"]
    except _QueryTooSlow:
        return TicketFlow(window=window, flow={}, open_by_severity={}, timed_out=True)

    labels = SeverityChoices.as_dict()
    ranked = sorted(severities, key=lambda row: -SEVERITY_WEIGHTS.get(row["severity"], 0))
    open_by_severity = (
        {"Open tickets": {labels.get(row["severity"], row["severity"]): row["total"] for row in ranked}}
        if ranked
        else {}
    )
    return TicketFlow(
        window=window,
        flow={
            "Opened": _by_day(opened, "total"),
            "Closed": _by_day(closed, "total"),
        },
        open_by_severity=open_by_severity,
        median_hours_to_close=(median.total_seconds() / 3600 if median is not None else None),
    )


def model_cost(*, user, days=None):
    """What the models cost over the window, split by purpose and by model, plus a failure rate.

    Money comes from `LLMUsageRecord.cost` and is never recomputed from token counts (rule D5). The
    service layer priced each call once, at call time, against the registry as it stood then; a
    price edited since must not retroactively rewrite what a past call cost.

    Clamped to `llm.usage_retention_days`, because `services.llm` deletes its own old records.
    """
    settings = get_settings()
    window = _window(
        days,
        settings,
        retention_days=_retention_days("llm", "usage_retention_days", LLM_USAGE_RETENTION_DEFAULT),
    )
    try:
        with _deadline(settings.query_timeout_seconds):
            # One pass, pivoted below, rather than three for the day series, the per-model table
            # and the counts. `LLMUsageRecord` is the fastest-growing table this page reads - a
            # triage call per ingested message - and each extra pass would carry the whole
            # restriction subquery with it. The grouped result is bounded by days x purposes x
            # models, which is the same "aggregate once, pivot in Python" shape used above.
            rows = list(
                visible_usage(user)
                .filter(called_at__gte=window.start, called_at__lte=window.end)
                .annotate(day=TruncDate("called_at"))
                .values("day", "purpose", "model__name")
                .annotate(total=Sum("cost"), calls=Count("pk"), failures=Count("pk", filter=Q(success=False)))
                .order_by("day")
            )
    except _QueryTooSlow:
        return ModelCost(window=window, cost_by_purpose={}, cost_by_model={}, timed_out=True)

    purposes = LLMPurposeChoices.as_dict()
    cost_by_purpose = {}
    cost_by_model = {}
    calls = failures = 0
    for row in rows:
        cost = float(row["total"] or 0)
        calls += row["calls"]
        failures += row["failures"]
        if row["model__name"]:
            cost_by_model[row["model__name"]] = cost_by_model.get(row["model__name"], 0.0) + cost
        if row["day"] is not None:
            series = cost_by_purpose.setdefault(purposes.get(row["purpose"], row["purpose"]), {})
            day = str(row["day"])
            series[day] = series.get(day, 0.0) + cost

    return ModelCost(
        window=window,
        # Ordered so neither the legend nor the table reshuffles between two renders of the same
        # window: purposes in the order `choices.py` declares them, models by what they cost.
        cost_by_purpose={label: cost_by_purpose[label] for label in purposes.values() if label in cost_by_purpose},
        cost_by_model=dict(sorted(cost_by_model.items(), key=lambda item: (-item[1], item[0]))),
        calls=calls,
        failures=failures,
    )


def agent_activity(*, user, days=None):
    """Agent runs per day by status, and the approve/deny split from the gate.

    Phase 4B's approval gate is a control. This is how anybody knows whether it is being used or
    rubber-stamped, which is a question the gate cannot answer about itself.

    No retention clamp: nothing prunes `AgentRun` or `AgentToolCall`.
    """
    settings = get_settings()
    window = _window(days, settings)
    try:
        with _deadline(settings.query_timeout_seconds):
            runs = list(
                visible_runs(user)
                .filter(started_at__gte=window.start, started_at__lte=window.end)
                .annotate(day=TruncDate("started_at"))
                .values("day", "status")
                .annotate(total=Count("pk"))
                .order_by("day")
            )
            decisions = list(
                visible_tool_calls(user)
                .filter(proposed_at__gte=window.start, proposed_at__lte=window.end)
                .values("status")
                .annotate(total=Count("pk"))
            )
    except _QueryTooSlow:
        return AgentActivity(window=window, runs_by_status={}, decisions={}, timed_out=True)

    run_labels = AgentRunStatusChoices.as_dict()
    runs_by_status = {}
    for row in runs:
        if row["day"] is None:
            continue
        name = run_labels.get(row["status"], row["status"])
        runs_by_status.setdefault(name, {})[str(row["day"])] = row["total"]

    call_labels = AgentToolCallStatusChoices.as_dict()
    counted = {call_labels.get(row["status"], row["status"]): row["total"] for row in decisions}
    return AgentActivity(
        window=window,
        runs_by_status=runs_by_status,
        decisions={"Tool calls": counted} if counted else {},
    )
