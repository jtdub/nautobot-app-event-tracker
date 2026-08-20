# Running the event consumer

Tickets can arrive on their own. A standalone process reads network events from a broker, decides
which of them are worth a ticket, and opens one through the same service layer a person uses.

Nothing here is required. An installation with no `ingestion` configuration consumes nothing, and
everything else in the app works exactly as before.

## What the process is

```shell
nautobot-server eventconsumer
```

It is a long-lived process, supervised alongside Nautobot's own — not a Job and not a Celery task.
A Job is a unit of work with an end, and one `JobResult` for a process that runs for weeks means
nothing; a Celery task that never returns holds a worker slot forever. See
[ADR 0005](../decisions/0005-standalone-consumer-process.md).

| Option | Effect |
| --- | --- |
| `--consumer kafka\|redis` | Override the configured broker implementation |
| `--topics a,b` | Consume a subset of the configured topics |
| `--max-messages N` | Exit cleanly after N messages — useful for a smoke test |
| `--dry-run` | Decide and report, writing no ticket and no counter, and acknowledging nothing |

Exit codes: **0** it was asked to stop, **1** it refused to start, **2** it could not carry on.
A supervisor should restart it in every case; only the second needs a person.

### A systemd unit

```ini
[Unit]
Description=Nautobot Event Tracker consumer
After=network-online.target nautobot.service
Wants=network-online.target

[Service]
Type=simple
User=nautobot
Group=nautobot
Environment="NAUTOBOT_CONFIG=/opt/nautobot/nautobot_config.py"
ExecStart=/opt/nautobot/bin/nautobot-server eventconsumer
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

`SIGTERM` is what a graceful stop looks like: the process finishes the message in flight,
acknowledges it, writes its counters and exits. A second signal exits immediately.

## Choosing a broker

| | Kafka | Redis pub/sub |
| --- | --- | --- |
| Replay after an outage | Yes | **No** |
| Messages published while the consumer is down | Delivered later | **Lost** |
| Several instances | Share a consumer group, splitting partitions | Every instance receives every message |
| Extra dependency | `pip install nautobot-event-tracker[kafka]` | None |

Kafka is the reference deployment. Redis is there for a lab, and the row that matters is the second
one: pub/sub delivers to whoever is listening at that moment, and no amount of configuration
changes that. See [ADR 0004](../decisions/0004-pluggable-event-broker-consumers.md).

Running two Redis consumers duplicates every message. That is harmless rather than correct — the
dedup rule collapses the duplicate into a recurrence — but it is not a way to scale.

## Configuration

```python
PLUGINS_CONFIG = {
    "nautobot_event_tracker": {
        "attachable_object_types": [...],
        "ingestion": {
            "consumer": "kafka",
            "kafka": {
                "bootstrap_servers": ["kafka-1:9092", "kafka-2:9092"],
                "group_id": "nautobot-event-tracker",
            },
            "topics": {
                "network.events": {
                    "field_map": {
                        "event_type": "event.type",
                        "title": "message",
                        "severity": "event.severity",
                        "description": "detail",
                        "occurred_at": "timestamp",
                    },
                    "defaults": {"event_type": "Unclassified", "severity": "minor"},
                    "severity_map": {
                        "0": "critical", "1": "critical", "2": "critical", "3": "major",
                        "4": "warning", "5": "minor", "6": "info", "7": "info",
                    },
                    "dedup_key_template": "{event.type}:{host}:{interface}",
                    "minimum_severity": "info",
                    "rate_limit": {"per_minute": 120, "burst": 240},
                    "rules": [
                        {"name": "lab-estate", "action": "drop",
                         "when": {"host": "^lab-"}},
                        {"name": "known-flapper", "action": "suppress",
                         "when": {"host": "^edge-rtr-07$", "event.type": "^Interface Down$"}},
                    ],
                    "resolve": [
                        {"name": "device", "path": "host",
                         "model": "dcim.device", "field": "name"},
                        {"name": "interface", "path": "interface",
                         "model": "dcim.interface", "field": "name",
                         "scope": {"device": "device"}},
                    ],
                },
            },
        },
    }
}
```

### Top-level settings

| Setting | Default | Description |
| --- | --- | --- |
| `consumer` | `redis` | Which implementation to run |
| `consumer_name` | `hostname:pid` | The name this process reports its counters under |
| `max_payload_bytes` | `65536` | Largest raw event stored on a ticket; a larger one is replaced by its size and a readable prefix |
| `event_type_cache_seconds` | `60` | How long the event type catalogue is held between reads |
| `max_retries` | `5` | Attempts at a message whose database write fails, before the process exits |
| `poll_timeout_seconds` | `1.0` | How long a poll waits before returning empty |
| `stats_bucket_seconds` | `300` | Width of one counter window |
| `stats_flush_seconds` | `10` | How often counters are written |
| `stats_retention_days` | `30` | Age at which counter rows are pruned |
| `resolve_cache_seconds` | `300` | How long the enrichment resolver trusts a lookup, hit or miss |
| `resolve_cache_entries` | `2000` | How many lookups it holds at once, evicting least recently used |

### Credentials

Set `external_integration` to the name of a Nautobot **External Integration** and the consumer takes
its address from that object's remote URL, and its username and password from the attached secrets
group. Credentials then live where the rest of the deployment's credentials live, and are rotated
the same way. The remote URL is rendered, so Jinja2 templating works there.

*SSL Verification* and *CA File Path* are honoured too: Kafka gets `ssl.ca.location` and
`enable.ssl.certificate.verification`, and Redis gets `ssl_ca_certs` or `ssl_cert_reqs` — the
latter only for a `rediss://` URL, since redis-py refuses an SSL keyword on a plaintext connection.

The plain `bootstrap_servers` and `url` settings are for a lab. They carry no TLS settings, and a
credential in one of them sits in `PLUGINS_CONFIG` unencrypted; the consumer strips it before
logging the URL, which limits the damage without making the practice supported.

For Kafka, `security_protocol` (`SASL_PLAINTEXT` or `SASL_SSL`) and `sasl_mechanism` (`PLAIN`,
`SCRAM-SHA-256` or `SCRAM-SHA-512`) select how those credentials are presented. Both are consulted
only when the integration supplies a username and password, and a broker reached over anything but
a private network wants `SASL_SSL`.

### Describing a topic's payload

A `field_map` says where each field lives, as a dotted path: `event.type` means
`payload["event"]["type"]`. A path that does not resolve falls back to `defaults`, and a missing
title falls back to the event type name.

`dedup_key_template` renders the idempotency key from the same paths — `{event.type}:{host}`. It is
**not** Python's `str.format`, so `{event.type}` means the path, not attribute access. If any path
in it is missing, the key comes out empty and the event opens its own ticket rather than joining an
arbitrary one.

`severity_map` translates the source system's severities into the app's five. Keys are compared as
strings, so a syslog severity arriving as `4` and one arriving as `"4"` hit the same entry.

### Attaching the objects an event names

A ticket saying `Interface ethernet-1/1 is down` on `leaf-01` carries those as strings. Nautobot
knows both of them as objects. A `resolve` block is how the ticket gets them: a list of rules, run
in order after the event has survived every filter and before its ticket is written.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique within the topic. A later rule scopes on this name |
| `path` | yes | Dotted path to the value naming the object, the same syntax `field_map` uses |
| `model` | yes | `app_label.model`. Must be on `attachable_object_types` |
| `field` | yes | The field to look it up by, case-insensitively |
| `scope` | no | `{field on this rule's model: name of an earlier rule}` |

**Why `scope` exists.** An interface name is unique per device, not globally: every switch in the
estate has an `ethernet-1/1`. Looking one up by name alone matches all of them, so the rule is told
which device — the one an earlier rule already found. A rule may only scope on a rule defined
before it, which is also why cycles are impossible.

**What it does when it cannot find something.** Nothing that costs you the ticket. A value that
matches no row, or matches two, attaches nothing and counts a miss; the event still becomes a
ticket, in full. Two of these are worth knowing about specifically:

- **A path the payload does not carry is a miss** — the rule and the payload disagree, and you
  should hear about it.
- **A path that is there and empty is not.** `"interface": ""` means the producer is telling you
  there is no interface in this message, which is the truth for an event about a BGP session.

**What it refuses to start with.** A model that is not on `attachable_object_types`, a field that
does not exist on it, a scope naming a later rule, or a scope on something that is not a relation.
All of it is checked before the first message, because a resolve rule that failed *during* a ticket
write would roll back the ticket rather than the attachment.

Resolution runs on a dry run too, unlike LLM triage: it spends nothing, and these rules are exactly
what a dry run is for. The reported line names what each message would attach.

The cache is worth a thought on a large estate. A lookup is held for `resolve_cache_seconds` — so a
device onboarded since the last read takes up to that long to start resolving — and at most
`resolve_cache_entries` are held at once. Misses are cached as well as hits, which is what keeps an
unknown hostname arriving ten thousand times from being ten thousand queries.

## What becomes a ticket

Every message runs through the same six checks, cheapest first, and the first one to decide wins:

1. **Is the topic configured?** If not, nothing else can be done with it.
2. **Is the event type known?** An unknown one becomes `defaults.event_type` — or is dropped, if
   `unknown_event_type` is set to `drop`.
3. **Is that event type enabled?** Disabling a type in the UI stops tickets being made for it,
   within `event_type_cache_seconds`.
4. **Is the severity at or above `minimum_severity`?**
5. **Does one of your `rules` fire?** Each rule's `when` is a mapping of path to regular
   expression, and every clause must match. `action: drop` discards the event entirely;
   `action: suppress` opens the ticket and puts it straight into **Suppressed**, so the noise is on
   the record without being work.
6. **Is the topic within its `rate_limit`?** A token bucket, per process — three instances admit
   three times as many.

What survives becomes a ticket, or joins an existing one if its dedup key matches an open ticket —
carrying whatever the `resolve` rules found as attached objects.

Two things worth knowing about suppression rules. They apply only to a ticket the event *opened*: a
rule firing on a later event will not pull a ticket somebody has already triaged out from under
them. And a rate limit dropping a burst means the recurrences in that burst are not counted, so
`event_count` under-reports during a storm.

### Tuning rules safely

```shell
nautobot-server eventconsumer --dry-run
```

Every message is decided and reported, and nothing is written or acknowledged — so with Kafka, the
consumer group's offsets do not move and the same messages are still there afterwards.

## Watching it work

**Apps → Event Tracker → Ingestion Stats** shows one row per consumer, topic and time window:
messages received, tickets opened, tickets joined, suppressed, dropped, errored, and the time of the
newest message. `enriched` counts events that attached at least one resolved object, and
`enrichment_misses` counts rule evaluations that found nothing — one event whose two rules both
missed is one event and two misses. Every drop is also counted under the rule or filter that refused it, which is the
first place to look when events are not becoming tickets.

The counting invariant is `received = errored + dropped + opened + joined`. Suppressed is not a
term in it: a suppressed message still opened or joined a ticket.

Counters are held in memory and written every `stats_flush_seconds`, so the database write rate does
not follow the message rate. A hard kill loses at most that interval. Rows older than
`stats_retention_days` are pruned by the consumer itself, so there is no scheduled job to set up.

## When something is wrong

**No tickets at all.** Check the process is running, then check Ingestion Stats. `received` at zero
means nothing is arriving from the broker; `received` rising with everything landing in `dropped`
means a filter is doing it, and `drops_by_reason` names which.

**`errored` is rising.** Messages are arriving that are not JSON objects. The log line for each
names the topic and offset. A message that cannot be parsed is counted, logged and discarded — it
is never retried, because it would not parse the second time either and a consumer that keeps
trying stops consuming anything else.

**The process exits with 2.** The database would not take a message after `max_retries`. The
message was **not** acknowledged and will be redelivered, so nothing is lost; fix the database and
the supervisor's restart picks up where it left off.

**Nothing is attached to the tickets.** Watch `enrichment_misses` on the stats page. Rising with
`enriched` at zero means the rules are running and finding nothing: either the payload path is
wrong — check it against a raw message — or the names in Nautobot are not the names the devices
log. The consumer logs each miss with the rule that made it and why. `enriched` and
`enrichment_misses` both at zero means no rule ran at all, which is a `resolve` block on the wrong
topic.

**Tickets are duplicated.** Two open tickets for the same problem means the dedup key is not
resolving — usually a path in `dedup_key_template` that the payload does not carry. A quick check:
those tickets will all have an event count of 1, and an empty dedup key on the detail page.
