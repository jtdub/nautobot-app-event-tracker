"""Ingestion configuration for the lab, imported by the development configuration.

Kept beside the lab rather than in `nautobot_config.py` so that the ordinary development stack -
which has no broker - is unaffected by its presence.

The field map here is the counterpart of `fluent-bit.conf` and `classify.lua`: those decide the
shape of the message, and this reads it. If the two disagree, tickets arrive as Unclassified or
with empty titles, and the acceptance check in the Phase 2.5 spec (section 9.5) is what catches it.
"""

LAB_INGESTION = {
    "consumer": "kafka",
    "consumer_name": "lab",
    "kafka": {
        "bootstrap_servers": ["redpanda:9092"],
        "group_id": "nautobot-event-tracker-lab",
    },
    # Short, so a developer watching the stats page sees a bucket roll while they are still looking.
    "stats_bucket_seconds": 60,
    "stats_flush_seconds": 5,
    "topics": {
        "network.events": {
            "field_map": {
                "event_type": "event.type",
                "title": "message",
                "severity": "event.severity",
                "occurred_at": "timestamp",
            },
            "defaults": {"event_type": "Unclassified"},
            # Syslog severities: 0-2 are the ones that wake somebody, 3 is an error, 4 a warning.
            "severity_map": {
                "0": "critical",
                "1": "critical",
                "2": "critical",
                "3": "major",
                "4": "warning",
                "5": "minor",
                "6": "info",
                "7": "info",
            },
            # Interface included, so two interfaces flapping on one device are two tickets.
            "dedup_key_template": "{event.type}:{host}:{interface}",
            # The lab is noisy at boot; without a floor the first ticket list is all informational
            # start-up chatter. Raise it to major to see only the breaks you cause on purpose.
            "minimum_severity": "warning",
            "rules": [
                # Overlaps the severity floor above, deliberately. The floor is the coarse tool and
                # the first thing anybody lowers - drop it to `info` to watch every message arrive
                # and the boot chatter comes back with it, except for this. SR Linux also logs some
                # application restarts at warning rather than notice, which the floor lets through.
                {
                    "name": "srlinux-boot-chatter",
                    "action": "drop",
                    "when": {"message": "(?i)(starting|initialized|application .* is now)"},
                },
            ],
        },
    },
}
