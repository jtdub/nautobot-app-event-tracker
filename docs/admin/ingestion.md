# Running the event consumer

The app can open tickets with no action from a person. A standalone process reads network events
from a broker. The process decides which events need a ticket. It opens each ticket through the same
service layer that a person uses.

You do not have to run the consumer. An installation with no `ingestion` setting consumes no events.
Everything else in the app works as before.

## What the process is

```shell
nautobot-server eventconsumer
```

The consumer is a process that runs for a long time. Supervise it in the same way as the Nautobot
processes. It is not a Job and not a Celery task, for two reasons:

- A Job is a unit of work with an end. One `JobResult` for a process that runs for weeks has no
  meaning.
- A Celery task that never returns holds a worker slot forever.

See [ADR 0005](../decisions/0005-standalone-consumer-process.md).

| Option | Effect |
| --- | --- |
| `--consumer kafka\|redis` | Use this broker implementation, not the configured one |
| `--topics a,b` | Consume only these configured topics |
| `--max-messages N` | Exit with code 0 after N messages. Use it for a smoke test |
| `--dry-run` | Decide and report. Write no ticket and no counter, and acknowledge no message |

The consumer uses these exit codes:

- **0**: The consumer stopped when asked.
- **1**: The consumer refused to start, or it received a second stop signal.
- **2**: The consumer could not continue.

Set the supervisor to restart the consumer after each exit code. After exit code **1** for a
configuration fault, the consumer refuses to start again until you correct the configuration. After
exit code **2**, correct the database.

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

`SIGTERM` stops the consumer after it finishes the current message. The consumer acknowledges that
message, writes its counters and exits. A second signal makes it exit immediately.

## Choose a broker {#choosing-a-broker}

| | Kafka | Redis pub/sub |
| --- | --- | --- |
| Replay after an outage | Yes | **No** |
| Messages published while the consumer is down | Delivered later | **Lost** |
| Several instances | Share a consumer group and split the partitions | Every instance receives every message |
| Extra dependency | `pip install nautobot-event-tracker[kafka]` | None |

Kafka is the reference deployment. Use Redis for a lab. The second row of the table is the most
important. Redis pub/sub delivers a message only to the consumers that listen at that moment. No
setting changes this. See [ADR 0004](../decisions/0004-pluggable-event-broker-consumers.md).

If you run two Redis consumers, each message arrives twice. The dedup rule joins the second copy to
the ticket as a recurrence, so this does no harm. Do not run two Redis consumers to scale.

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
| `consumer` | `redis` | The broker implementation to run |
| `consumer_name` | `hostname:pid` | The name that this process uses for its counters |
| `max_payload_bytes` | `65536` | The largest raw event that a ticket stores. A larger event becomes its size and a readable prefix |
| `event_type_cache_seconds` | `60` | How long the consumer keeps the event type catalogue between reads |
| `max_retries` | `5` | The attempts to handle a message when its database write fails. The consumer exits after the last attempt |
| `poll_timeout_seconds` | `1.0` | How long a poll waits before it returns empty |
| `stats_bucket_seconds` | `300` | The width of one counter window |
| `stats_flush_seconds` | `10` | How often the consumer writes the counters |
| `stats_retention_days` | `30` | The age at which the consumer prunes counter rows |
| `resolve_cache_seconds` | `300` | How long the enrichment resolver trusts a lookup, for a hit or a miss |
| `resolve_cache_entries` | `2000` | The number of lookups that the resolver holds at once. It removes the least recently used lookup first |

### Credentials

Set `external_integration` in the `kafka` or `redis` block to the name of a Nautobot **External
Integration**. The consumer then takes its address from the remote URL of that object. It takes its
username and password from the secrets group of that object. The credentials are then in the same
place as the other credentials of the deployment. You rotate them in the same way. Nautobot renders
the remote URL, so you can use Jinja2 templates in it.

The consumer also uses the **Verify SSL** and **CA file path** fields of the External Integration:

- For Kafka, **CA file path** sets `ssl.ca.location`. If you turn off **Verify SSL**, the consumer
  sets `enable.ssl.certificate.verification` to false.
- For Redis, **CA file path** sets `ssl_ca_certs`. If you turn off **Verify SSL**, the consumer sets
  `ssl_cert_reqs` to none.
- For Redis, the consumer sets these keywords only for a `rediss://` URL. The redis-py library
  refuses an SSL keyword on a plaintext connection.

The `bootstrap_servers` and `url` settings are for a lab. They have no TLS settings.

!!! warning
    A credential in `bootstrap_servers` or `url` stays in `PLUGINS_CONFIG` as plain text. Use an
    External Integration for each credential.

The consumer removes the credential from the URL before it writes the URL to the log. This limits
the harm, but the app does not support a credential in a URL.

For Kafka, two settings control how the consumer sends the credentials:

- `security_protocol`: `SASL_PLAINTEXT` or `SASL_SSL`.
- `sasl_mechanism`: `PLAIN`, `SCRAM-SHA-256` or `SCRAM-SHA-512`.

The consumer uses both settings only when the integration supplies a username and a password. Use
`SASL_SSL` for a broker that you reach over a network that is not private.

### Describe the payload of a topic {#describing-a-topics-payload}

A `field_map` gives the location of each field as a dotted path. For example, `event.type` means
`payload["event"]["type"]`. If a path does not resolve, the consumer uses the value in `defaults`. If
the title is absent, the consumer uses the event type name.

`dedup_key_template` makes the idempotency key from the same paths, for example
`{event.type}:{host}`. It is **not** Python `str.format`. `{event.type}` means the path, not
attribute access. If a path in the template is absent, the key is empty. The event then opens its own
ticket and does not join an unrelated ticket.

`severity_map` translates the severities of the source system into the five severities of the app.
The consumer compares keys as strings. A severity `4` and a severity `"4"` therefore use the same
entry.

### Attach the objects that an event names {#attaching-the-objects-an-event-names}

A ticket can have the title `Interface ethernet-1/1 is down` for the host `leaf-01`. The ticket holds
both names as strings. Nautobot knows both names as objects.

A `resolve` block attaches those objects to the ticket. It is a list of rules. The consumer runs the
rules in order. It runs them after the event passes every filter, and before it writes the ticket.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | A name that is unique within the topic. A later rule uses this name in `scope` |
| `path` | yes | The dotted path to the value that names the object. It uses the same syntax as `field_map` |
| `model` | yes | `app_label.model`. The model must be in `attachable_object_types` |
| `field` | yes | The field to look up the object by. The lookup ignores case |
| `scope` | no | `{field on this rule's model: name of an earlier rule}` |

**Why `scope` exists.** An interface name is unique for each device, not globally. Every switch in
the network has an `ethernet-1/1`. A lookup by name alone matches all of them. A `scope` gives the
device that an earlier rule found. A rule can scope only on a rule that is before it, so cycles
cannot occur.

**What happens when the consumer cannot find an object.** The consumer does not lose the ticket. If a
value matches no row, or matches two rows, the consumer attaches nothing and counts a miss. The event
still opens a ticket, in full. Note these two cases:

- **A path that the payload does not carry is a miss.** The rule and the payload disagree. The
  counter shows the miss.
- **A path that is in the payload and is empty is not a miss.** The value `"interface": ""` means
  that the message has no interface. This is correct for an event about a BGP session.

**When the consumer refuses to start.** The consumer refuses to start if it finds one of these
faults:

- A model that is not in `attachable_object_types`.
- A field that does not exist on the model.
- A scope that names a later rule.
- A scope on a field that is not a relation.

The consumer checks all of these before the first message. A resolve rule that fails during a ticket
write would roll back the ticket, not only the attachment.

Resolution also runs in a dry run, but LLM triage does not. Resolution has no cost, and a dry run is
the best way to tune these rules. The report line names the objects that each message would attach.

**The cache.** On a large network, consider the cache. The resolver holds a lookup for
`resolve_cache_seconds`. A device that you add to Nautobot can need that much time before it
resolves. The resolver holds at most `resolve_cache_entries` lookups at once. It caches misses and
hits. An unknown host name that arrives 10,000 times therefore does not cause 10,000 queries.

## What becomes a ticket

Each message passes through the same six checks, from the cheapest to the most expensive. The first
check that decides the outcome applies:

1. **Is the topic configured?** If not, the consumer drops the message.
2. **Is the event type known?** The consumer changes an unknown event type to `defaults.event_type`.
   If `unknown_event_type` is `drop`, the consumer drops the event.
3. **Is that event type enabled?** If you turn off **Enabled** for an event type in the UI, the
   consumer drops events of that type. The change applies after at most `event_type_cache_seconds`
   seconds.
4. **Is the severity at or above `minimum_severity`?** If not, the consumer drops the event.
5. **Does one of your `rules` fire?** The `when` of a rule is a map of paths to regular
   expressions. Every clause must match.
    - `action: drop` drops the event.
    - `action: suppress` opens the ticket and transitions it to **Suppressed**. The app keeps the
      ticket, but a person does not need to work on it.
6. **Is the topic within its `rate_limit`?** The limit is a token bucket for each process. Three
   instances admit three times as many events.

An event that passes all six checks opens a ticket. If its dedup key matches an open ticket, the
event joins that ticket instead. The consumer attaches to the ticket the objects that the `resolve`
rules found.

Note these two limits:

- A suppression rule applies only to a ticket that the event opened. A rule that fires on a later
  event does not transition the ticket that already exists. Therefore, a suppression rule cannot change a
  ticket that a person already works on.
- A rate limit that drops a burst also drops the recurrences in that burst. The consumer does not
  count them, so `event_count` is too low during a burst of events.

### Tune rules safely {#tuning-rules-safely}

Use a dry run to test your rules on live events:

```shell
nautobot-server eventconsumer --dry-run
```

The consumer decides and reports each message. It writes nothing and acknowledges nothing. With
Kafka, the offsets of the consumer group do not move. The same messages are still on the broker
afterward.

## Check that the consumer works {#watching-it-work}

The **Ingestion Stats** list shows one row for each consumer, topic and time window.

![The Ingestion Stats list, with one row for each consumer, topic and time window](../media/ss_ingestion_stats_light.png#only-light)
![The Ingestion Stats list, with one row for each consumer, topic and time window](../media/ss_ingestion_stats_dark.png#only-dark)

Open **Apps > Event Tracker > Ingestion Stats**. Each row shows the messages received, the tickets
opened, the tickets joined, and the messages suppressed, dropped and errored. It also shows the time
of the newest message.

- `enriched` counts the events that have at least one object that the resolver found.
- `enrichment_misses` counts the rule evaluations that found nothing. An event with two rules that
  both miss is one event and two misses.

The consumer also counts each drop under the rule or filter that refused the event
(`drops_by_reason`). Look there first when events do not become tickets.

The counters follow this equation: `received = errored + dropped + tickets_opened + tickets_joined`. The counter
`suppressed` is not in the equation, because a suppressed message also opened or joined a ticket.

The consumer keeps the counters in memory. It writes them every `stats_flush_seconds`, so the
database write rate does not depend on the message rate. A hard kill (for example `kill -9`) loses
the counters of one flush interval at most. The consumer prunes the rows that are older than
`stats_retention_days`, so you do not need a scheduled job.

## When something is wrong

**No tickets at all.** Make sure that the consumer runs. Then open **Ingestion Stats**. If `received`
is zero, no messages arrive from the broker. If `received` rises and all messages go to `dropped`,
a filter drops them. `drops_by_reason` names the filter.

**`errored` rises.** Messages arrive that are not JSON objects, or that the consumer cannot decode.
The consumer writes a log line for each one, with the topic and the offset. It counts the message,
logs it and discards it. It never retries the message, because the message does not parse the second
time. A consumer that retries a bad message cannot read any other message.

**The process exits with code 2.** The database did not accept a message after `max_retries`
attempts. The consumer did **not** acknowledge the message. With Kafka, the broker delivers it
again, so no message is lost. With Redis pub/sub, the message is lost. Fix the database. With Kafka,
the restart by the supervisor then continues from that message.

**The consumer attaches no objects to the tickets.** Look at `enrichment_misses` on the **Ingestion
Stats** list. If it rises and `enriched` stays at zero, the rules run but find nothing. Then one of
these is true:

- The payload path is wrong. Compare it with a raw message.
- The names in Nautobot are not the names that the devices write in their logs.

The consumer logs each miss with the name of the rule and the reason. If `enriched` and
`enrichment_misses` are both zero, no rule ran. Check that the `resolve` block is on the correct
topic.

**Two tickets exist for the same problem.** Two open tickets for the same problem mean that the
dedup key does not resolve. The usual cause is a path in `dedup_key_template` that the payload does
not carry. To check, open the ticket list. Each of these tickets has an **Event count** of 1. In the
REST API, the `dedup_key` field of each of these tickets is empty.
