"""Broker implementations, and the name an operator uses to choose one."""

from django.core.exceptions import ImproperlyConfigured

from nautobot_event_tracker.ingestion.consumers.base import BrokerMessage, EventConsumer, connection_details
from nautobot_event_tracker.ingestion.consumers.kafka import KafkaEventConsumer
from nautobot_event_tracker.ingestion.consumers.redis import RedisEventConsumer

#: The `consumer` setting's values. Kafka's client is an optional extra, but the class imports
#: without it, so an operator naming a consumer they cannot run gets an installation hint rather
#: than a lookup failure.
CONSUMERS = {
    "kafka": KafkaEventConsumer,
    "redis": RedisEventConsumer,
}

__all__ = (
    "CONSUMERS",
    "BrokerMessage",
    "EventConsumer",
    "KafkaEventConsumer",
    "RedisEventConsumer",
    "connection_details",
    "get_consumer_class",
)


def get_consumer_class(name):
    """Return the consumer class this name selects."""
    try:
        return CONSUMERS[name]
    except KeyError as error:
        raise ImproperlyConfigured(
            f"Unknown ingestion consumer '{name}'. Available: {', '.join(sorted(CONSUMERS))}."
        ) from error
