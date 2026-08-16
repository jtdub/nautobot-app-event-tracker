#!/usr/bin/env python
"""Publish an event onto whatever broker this environment is configured to consume from.

    invoke send-test-event
    invoke send-test-event --event bgp --host spine-01 --count 3

The point is to make Phase 2 demonstrable on any machine: `invoke start`, `invoke eventconsumer` in
another terminal, then this, and a ticket appears. The containerlab lab produces the same messages
from a device that means them; this produces them because you asked. Everything downstream of the
broker - the field map, the pre-filter, dedup, the counters - is identical either way, which is what
makes this worth having and also what it cannot tell you: whether a real SR Linux says any of it.

It reads the app's own ingestion configuration, so it publishes where the consumer is listening,
in the shape the field map expects, with no second copy of either to keep in step.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

#: The messages, worded as the devices word them, keyed by the name this script takes. Severities
#: are syslog's, which is what the lab's severity map is keyed on: 3 is an error, 4 a warning.
EVENTS = {
    "interface": ("Interface {interface} is down", "3", True),
    "bgp": ("bgp neighbor {peer} session state changed to down", "3", False),
    "unreachable": ("Peer {peer} is unreachable, keepalive expired", "2", False),
    "optical": ("Optical input on {interface} crossed the low threshold", "4", True),
    "cpu": ("System cpu utilization crossed the threshold, now 94%", "4", False),
    "drift": ("Commit accepted by user admin", "5", False),
    "unknown": ("Something happened that no pattern here describes", "4", False),
}


def main(arguments):
    """Publish `--count` copies of one event, and say where they went."""
    from nautobot_event_tracker.ingestion import config  # pylint: disable=import-outside-toplevel

    loaded = config.load(require_topics=True)
    topic = arguments.topic or loaded.topic_names[0]

    payloads = [_payload(arguments, number) for number in range(arguments.count)]
    publish = {"redis": _publish_to_redis, "kafka": _publish_to_kafka}[loaded.consumer]
    publish(loaded, topic, payloads)

    print(f"Published {len(payloads)} message(s) to '{topic}' on the {loaded.consumer} broker.")
    print(f"  {payloads[0]}")
    if loaded.consumer == "redis":
        print("\nRedis pub/sub keeps nothing: if the consumer was not running, this went nowhere.")


def _payload(arguments, number):
    """One message, in the shape the lab's syslog bridge produces."""
    text, severity, names_an_interface = EVENTS[arguments.event]
    message = text.format(interface=arguments.interface, peer=arguments.peer)
    return json.dumps(
        {
            "host": arguments.host,
            "message": message,
            # Empty rather than absent when the message names no interface, exactly as
            # `classify.lua` does it, so the dedup key still resolves. See docs/dev/lab.md.
            "interface": arguments.interface if names_an_interface else "",
            "event": {"type": arguments.type or _type_of(arguments.event), "severity": severity},
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            # So that `--count 3` is three deliveries of one event rather than three events: the
            # dedup key ignores this, which is the point - one ticket, three occurrences.
            "sequence": number,
        }
    )


def _type_of(event):
    """The seeded event type a message of this kind implies, as `classify.lua` decides it."""
    return {
        "interface": "Interface Down",
        "bgp": "BGP Session Down",
        "unreachable": "Device Unreachable",
        "optical": "Optical Degradation",
        "cpu": "High CPU Utilization",
        "drift": "Configuration Drift",
        "unknown": "Unclassified",
    }[event]


def _publish_to_redis(loaded, topic, payloads):
    """Publish to the channel the Redis consumer subscribes to."""
    import redis  # pylint: disable=import-outside-toplevel

    from nautobot_event_tracker.ingestion.consumers.base import (  # pylint: disable=import-outside-toplevel
        connection_details,
    )

    url, username, password = connection_details(loaded.consumer_settings, url_key="url")
    credentials = {key: value for key, value in (("username", username), ("password", password)) if value}
    client = redis.Redis.from_url(url, **credentials)
    for payload in payloads:
        client.publish(topic, payload)


def _publish_to_kafka(loaded, topic, payloads):
    """Produce to the topic the Kafka consumer subscribes to."""
    try:
        # An optional extra, exactly as it is for the consumer: an environment consuming from Redis
        # installs nothing it does not use. The development image installs every extra.
        from confluent_kafka import Producer  # pylint: disable=import-outside-toplevel,import-error
    except ImportError:
        sys.exit("confluent-kafka is not installed. `poetry install --extras kafka`, or use the development image.")

    from nautobot_event_tracker.ingestion.consumers.base import (  # pylint: disable=import-outside-toplevel
        connection_details,
    )

    url, username, password = connection_details(loaded.consumer_settings, url_key="bootstrap_servers")
    settings = {"bootstrap.servers": ",".join(url) if isinstance(url, (list, tuple)) else url}
    if username and password:
        settings.update({"sasl.username": username, "sasl.password": password})

    producer = Producer(settings)
    for payload in payloads:
        producer.produce(topic, payload.encode("utf-8"))
    producer.flush(10)


def parse(argv):
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", choices=sorted(EVENTS), default="interface", help="which message to send.")
    parser.add_argument("--host", default="leaf-01", help="the device it comes from.")
    parser.add_argument("--interface", default="ethernet-1/1", help="the interface it names.")
    parser.add_argument("--peer", default="10.1.1.1", help="the peer it names, for the BGP messages.")
    parser.add_argument("--type", default="", help="override the event type the message implies.")
    parser.add_argument("--topic", default="", help="publish to this topic instead of the first configured one.")
    parser.add_argument("--count", type=int, default=1, help="how many copies to send.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    import nautobot
    from nautobot.core.cli import get_config_path

    if not os.path.exists(get_config_path()):
        sys.exit(f"No Nautobot configuration at {get_config_path()}. Set NAUTOBOT_CONFIG and try again.")

    nautobot.setup()
    main(parse(sys.argv[1:]))
