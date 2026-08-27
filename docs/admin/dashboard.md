# The Analytics Dashboard

The app has been recording for four phases. `IngestionStats` has bucketed the ingestion funnel
since Phase 2, `LLMUsageRecord` has priced every model call since Phase 3, and `AgentRun` has
recorded every investigation since Phase 4B. Until now the only way to see any of it was a list
view or a GraphQL query written by hand.

The dashboard lives at **Apps → Event Tracker → Analytics**
(`/plugins/event-tracker/dashboard/`) and answers four questions:

- **Is ingestion keeping up, and what is it dropping?**
- **How many tickets, in what state, and how long do they take?**
- **What is this costing, by model and by purpose?**
- **Are agents being approved, or rubber-stamped?**

## What this does not do

**It writes nothing.** No model, no counter, no cache row, no migration beyond three indexes. A cache
table is the obvious thing to reach for and this app refuses it: it would be stale in exactly the
case somebody is staring at the page, which is during an incident.

**It calls no language model.** The dashboard shows numbers a person reads. It does not summarize
them. "Describe this month's incidents" is a different feature with a different argument behind it,
and the test suite asserts the import direction that keeps it out.

**It has no per-person breakdown.** A chart of tickets closed per person is a
performance-management tool wearing an operations hat, and this app has no mandate for that.

## Switching it on

It is already on. This is the only settings block in the app that defaults to enabled, because it
reaches no network and spends no money — it runs a handful of `GROUP BY` queries against tables
your deployment already has.

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "dashboard": {
            "enabled": True,
            "default_window_days": 7,
            "max_window_days": 90,
            "query_timeout_seconds": 10,
        },
    },
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `True` | Whether the page, its route and its menu item exist at all. |
| `default_window_days` | `7` | The window the page opens on. |
| `max_window_days` | `90` | The largest window anybody may ask for. |
| `query_timeout_seconds` | `10` | Past this a panel reports that it took too long. |

Setting `enabled` to `False` removes the route and the menu item rather than leaving a page that
returns 404. Restart Nautobot after you change it: the route is built when the app loads.

## Who sees what

**Every number on the page is computed over what the requesting user may already read.** That is
worth stating plainly, because an aggregate leaks without returning anything. A user constrained by
an ObjectPermission to one tenant's tickets, shown a count of 4,812 open tickets, has learned the
size of the estate they were scoped out of. A severity pie tells them its shape.

So:

- Ticket counts, the severity split and time-to-close follow `view_eventticket`, including any
  constraint on it.
- Costs follow the **ticket**, not `view_llmusagerecord`. A usage record names its ticket, so a
  cost chart built from unrestricted records is a ticket-existence oracle with a price attached.
  A call that names no ticket is counted for everybody, because it discloses nothing.
- Agent runs and tool-call decisions follow the ticket too, through the run.
- Ingestion counters follow `view_ingestionstats` alone. A counter row names a consumer and a
  topic, not a ticket, so there is no parent for it to follow.

The page itself needs no permission beyond being logged in. A user with none simply sees zeroes,
which is the honest answer rather than a 403 that confirms there is something there.

## Retention shortens the window

Two of the tables prune themselves, and the dashboard will not draw data that has been deleted as
though it were quiet:

| Panel group | Pruned by | Setting | Default |
| --- | --- | --- | --- |
| Ingestion health | `StatsRecorder` | `ingestion.stats_retention_days` | 30 days |
| Model cost | `services.llm` | `llm.usage_retention_days` | 90 days |

Ask for ninety days on a stock install and the ingestion panels report **"last 30 days (retention;
90 requested)"** under their titles. That is not an error. Raise `stats_retention_days` if you want
a longer view of the funnel, and be aware that the table then grows for as long as you asked for.

Ticket flow and agent activity are not clamped. Nothing prunes `EventTicket`, `AgentRun` or
`AgentToolCall`.

The severity pie is not windowed at all, and deliberately: "which tickets are open right now" has no
time bound to give it. It is the one query on the page that grows with the whole open backlog, which
is why it has an index of its own.

## Reading the cost figures

The money comes from `LLMUsageRecord.cost` and is **USD**, as that field's own help text says. It is
what the service layer computed at call time, against the model registry as it stood then. Editing a
model's registered prices today does not rewrite what last month's calls cost, which is the point:
the chart is accounting, not an estimate.

If you have registered providers priced in different currencies, the sum is meaningless and nothing
will warn you. Register prices in one currency.

## When a panel says it took too long

A panel that exceeds `query_timeout_seconds` draws nothing and puts **"This took too long to draw.
Try a shorter window."** under its title. The rest of the page still renders.

Treat that as a defect rather than as a setting to raise. In order of what to try:

1. **A shorter window.** The default of seven days is what the page is built around.
2. **Check the indexes landed.** Migration `0011_analytics_indexes` adds three indexes on
   `EventTicket`: `created` and `closed_at` for the ticket-flow range scans, and
   `(status, severity)` for the open-tickets pie. That last one matters most if the slow panel is
   **Open tickets by severity** — it asks which tickets are open *right now*, so it is the one
   query on the page with no time bound and nothing to bound it by. Everything else the page groups
   by was already indexed.
3. **Report it.** Raising the timeout hides the problem, and a persistently slow panel becomes
   invisible furniture rather than something somebody fixes.

## Measuring it yourself

The page is meant to render in under two seconds against 100,000 tickets and 1,000,000 usage
records. The test suite pins the query count rather than the wall clock, because a clock assertion
in CI fails for reasons unrelated to this code. To take the real measurement on your own hardware:

```bash
invoke nbshell
```

```python
import time
from django.test import Client
from django.contrib.auth import get_user_model

client = Client()
client.force_login(get_user_model().objects.filter(is_superuser=True).first())

start = time.perf_counter()
client.get("/plugins/event-tracker/dashboard/?days=7")
print(f"{time.perf_counter() - start:.2f}s")
```

Compare a seven-day window against a ninety-day one. If the short window is slow, the problem is an
index; if only the long one is, the problem is the window.

## Further reading

- [Phase 5B specification](../specs/phase-5b-analytics.md) — rules D1 to D8, and the arguments.
- [ADR 0008 — UI Component Framework only](../decisions/0008-ui-component-framework-only.md) — why
  this page is the app's one template.
- [Running the Event Consumer](ingestion.md) — where the ingestion counters come from.
- [Configuring LLM Providers](llm.md) — where the prices come from.
