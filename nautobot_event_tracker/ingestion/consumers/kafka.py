"""The Kafka consumer: the reference implementation, and the only one that can replay.

`confluent-kafka` is an optional extra, so this module imports without it and says something
useful when it is missing. A deployment consuming from Redis, or not consuming at all, installs
nothing it does not use.
"""

import logging
from datetime import datetime, timezone

from django.core.exceptions import ImproperlyConfigured

from nautobot_event_tracker.ingestion.consumers.base import BrokerMessage, EventConsumer, connection_details

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "The Kafka consumer needs the 'kafka' extra: pip install nautobot-event-tracker[kafka]. "
    "Alternatively set the ingestion consumer to 'redis'."
)


class KafkaEventConsumer(EventConsumer):
    """Kafka, with offsets committed only after a message has been fully handled."""

    supports_replay = True
    settings_key = "kafka"

    def __init__(self, *, settings, topics):
        """Hold the settings; the client is built in `connect()`."""
        super().__init__(settings=settings, topics=topics)
        self._consumer = None

    def connect(self):
        """Build the client and subscribe to the configured topics."""
        consumer_class = _import_consumer()
        connection = connection_details(self.settings, url_key="bootstrap_servers")
        servers, username, password = connection.url, connection.username, connection.password
        if not servers:
            raise ImproperlyConfigured("Kafka needs bootstrap_servers, or an external integration naming them.")

        config = {
            "bootstrap.servers": ",".join(servers) if isinstance(servers, (list, tuple)) else str(servers),
            "group.id": self.settings["group_id"],
            # Offsets are ours to commit, in `acknowledge()`, after the ticket transaction has
            # committed. Auto-commit would move them on a timer, turning a crash mid-message from a
            # redelivery into a loss.
            "enable.auto.commit": False,
            # A new group starts at the beginning rather than skipping whatever is already queued:
            # events that arrived while nobody was consuming are exactly the ones worth having.
            "auto.offset.reset": "earliest",
        }
        if username and password:
            config.update(
                {
                    "security.protocol": self.settings.get("security_protocol", "SASL_PLAINTEXT"),
                    "sasl.mechanism": self.settings.get("sasl_mechanism", "PLAIN"),
                    "sasl.username": username,
                    "sasl.password": password,
                }
            )

        # librdkafka accepts both regardless of protocol and applies them only when the connection
        # is encrypted, so there is nothing to gate on here - unlike redis-py, which refuses an SSL
        # keyword on a plaintext connection.
        if not connection.verify_ssl:
            config["enable.ssl.certificate.verification"] = False
        elif connection.ca_file_path:
            config["ssl.ca.location"] = connection.ca_file_path

        self._consumer = consumer_class(config)
        self._consumer.subscribe(list(self.topics))
        logger.info("Subscribed to %s on %s", ", ".join(self.topics), config["bootstrap.servers"])

    def poll(self, timeout):
        """Return the next message, or None on a timeout."""
        raw = self._consumer.poll(timeout)
        if raw is None:
            return None
        if raw.error() is not None:
            # Errors here are partition-level events - end of partition, a rebalance in progress -
            # rather than message failures. Logging and returning None sends the loop round again.
            logger.warning("Kafka reported %s", raw.error())
            return None
        return BrokerMessage(
            topic=raw.topic(),
            value=raw.value(),
            key=raw.key().decode("utf-8", "replace") if raw.key() else None,
            # Both, always: an offset counts within its partition, so one without the other names
            # no particular message (`BrokerMessage.identity`).
            partition=raw.partition(),
            offset=raw.offset(),
            timestamp=_timestamp(raw),
        )

    def acknowledge(self, message):
        """Commit this message's offset, now that its ticket is committed too."""
        self._consumer.commit(asynchronous=False)

    def close(self):
        """Leave the group cleanly, so the partitions are reassigned without waiting for a timeout."""
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None


def _timestamp(raw):
    """The broker's timestamp for a message, when it has one."""
    kind, value = raw.timestamp()
    if not value or kind == 0:  # TIMESTAMP_NOT_AVAILABLE
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _import_consumer():
    """Import the client, or explain how to install it."""
    try:
        from confluent_kafka import Consumer  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        raise ImproperlyConfigured(INSTALL_HINT) from error
    return Consumer
