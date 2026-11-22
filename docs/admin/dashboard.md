# The Analytics Dashboard

The dashboard shows what the app records. It reads the ingestion funnel (`IngestionStats`), the cost of each model
call (`LLMUsageRecord`), and each agent investigation (`AgentRun`). You can also see these records in a list view,
or in a GraphQL query that you write by hand.

The dashboard is at **Apps > Event Tracker > Analytics** (`/plugins/event-tracker/dashboard/`). It answers four
questions:

- **Is ingestion keeping up, and what does it drop?**
- **How many tickets are there, in what state, and how long do they take?**
- **What does this cost, by model and by purpose?**
- **How do people decide on agent tool calls?**

The dashboard shows the answers in charts and tables.

![The Event Tracker Analytics page, with the ingestion funnel and the open tickets by severity](../media/ss_dashboard_light.png#only-light)
![The Event Tracker Analytics page, with the ingestion funnel and the open tickets by severity](../media/ss_dashboard_dark.png#only-dark)

## What this does not do

**It writes nothing.** It has no model, no counter, no cache row, and no migration except three indexes. A cache
table is the obvious choice, and the app refuses it. A cache table would be stale in the case that matters most:
when someone looks at the page during an incident.

**It calls no language model.** The dashboard shows numbers that a person reads. It does not summarize them. A
feature that describes the incidents of a month needs a different design. The test suite checks the import
direction that keeps model calls out of the dashboard.

**It has no per-person breakdown.** A chart of tickets closed for each person measures the performance of people. This app does not have that
purpose.

## Turn the dashboard on or off {#switching-it-on}

The dashboard is on by default. It is the only block of settings in the app that is on by default. It uses no
network and spends no money. It runs a few `GROUP BY` queries against tables that your deployment already has.

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
| `enabled` | `True` | If `False`, the page, its route, and its menu item do not exist. |
| `default_window_days` | `7` | The number of days that the page shows when it opens. |
| `max_window_days` | `90` | The largest window that a user can ask for. |
| `query_timeout_seconds` | `10` | After this time, a panel reports that it took too long. |

If you set `enabled` to `False`, the app removes the route and the menu item. No menu item stays that leads to a
missing page. Restart Nautobot after you change `enabled`, because Nautobot builds the route when the app loads.

## Who sees what

**Every number on the page covers only what the user can already read.** An aggregate can leak data even if it
returns no record. Example: an ObjectPermission limits a user to the tickets of one tenant. The page shows this
user a count of 4,812 open tickets. The user now knows the size of the estate that the permission hides. A
severity pie shows the shape of it.

The page follows these rules:

- Ticket counts, the severity split, and time to close follow `view_eventticket`, with each constraint on it.
- Costs follow the ticket, not `view_llmusagerecord`. A usage record names its ticket. A cost chart from
  unrestricted records would show that a ticket exists, with a price. A call that names no ticket counts for
  everybody, because it discloses nothing.

- Agent runs and tool call decisions also follow the ticket, through the run.
- Ingestion counters follow `view_ingestionstats` only. A counter row names a consumer and a topic, not a ticket.
  It has no parent to follow.

The user must log in. The page needs no other permission. A user with no permission sees zeroes. This is the
honest answer. A 403 would confirm that something exists.

## Retention shortens the window

Two of the tables prune themselves. The dashboard does not show deleted data as a period with no activity:

| Panel group | Pruned by | Setting | Default |
| --- | --- | --- | --- |
| Ingestion health | `StatsRecorder` | `ingestion.stats_retention_days` | 30 days |
| Model cost | `services.llm` | `llm.usage_retention_days` | 90 days |

If you ask for 90 days on a default install, the ingestion panels show **"last 30 days (retention; 90
requested)"** under their titles. This is not an error. To see more of the funnel, raise
`ingestion.stats_retention_days`. The table then holds more rows.

Retention does not shorten the window of ticket flow or of agent activity. Nothing prunes `EventTicket`,
`AgentRun`, or `AgentToolCall`.

The severity pie has no window, on purpose. The question "which tickets are open now" has no time bound. This is
the one query on the page that grows with the whole open backlog. For this reason, it has an index of its own.

## Read the cost figures {#reading-the-cost-figures}

The money comes from `LLMUsageRecord.cost`. The help text of the field says that the unit is **USD**. The service
layer computed the cost at call time, against the model registry as it was then. If you edit the registered price
of a model, the cost of earlier calls does not change. The chart is accounting, not an estimate.

If you register providers with prices in different currencies, the sum has no meaning, and the app gives no
warning. Register all prices in one currency.

## When a panel says it took too long

A panel that exceeds `query_timeout_seconds` draws nothing. It shows **"This took too long to draw. Try a shorter
window."** under its title. The rest of the page still draws.

Treat this as a defect. Do not raise the setting. Try these steps in this order:

1. **Use a shorter window.** The page is built for the default window of seven days.
2. **Check that the indexes exist.** Migration `0011_analytics_indexes` adds three indexes on `EventTicket`:

    - `created` and `closed_at`, for the range scans of ticket flow.
    - `(status, severity)`, for the open tickets pie.

    The last index matters most when the slow panel is **Open tickets by severity**. That panel asks which tickets
    are open now. It is the one query on the page with no time bound. Every other column that the page groups by
    already has an index.

3. **Report the problem.** A higher timeout hides the problem. A panel that is slow all the time becomes normal, and
    nobody fixes it.

## Measure the speed yourself {#measuring-it-yourself}

The page is designed to draw in under two seconds against 100,000 tickets and 1,000,000 usage records. The test suite checks
the number of queries, not the time. A clock check in CI fails for reasons that this code does not cause. To
measure the time on your own hardware, run this command:

```bash
invoke nbshell
```

Then run this code in the shell:

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

Compare a window of seven days with a window of 90 days. If the short window is slow, the problem is an index. If
only the long window is slow, the problem is the window.

## Further reading

- [Phase 5B specification](../specs/phase-5b-analytics.md): rules D1 to D8, and the arguments.
- [ADR 0008: UI Component Framework only](../decisions/0008-ui-component-framework-only.md): why this page is the
  one template of the app.
- [Running the Event Consumer](ingestion.md): where the ingestion counters come from.
- [Configuring LLM Providers](llm.md): where the prices come from.
