"""The Redis pub/sub consumer: for development and lab use, and honest about why.

Redis pub/sub delivers to whoever is listening at that moment. A message published while this
process is down is gone - not delayed, not replayable, gone. That is the whole difference from
Kafka, and no amount of interface tidiness changes it.
"""

import logging
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured

from nautobot_event_tracker.ingestion.consumers.base import BrokerMessage, EventConsumer, connection_details

logger = logging.getLogger(__name__)


def _tls_options(url, connection):
    """The integration's TLS settings, in the keywords redis-py takes - and only when it takes them.

    Gated on the scheme because redis-py builds a plain `Connection` for `redis://`, which accepts
    no SSL keyword at all and raises on one. An operator who sets a CA path and then points at an
    unencrypted URL gets no TLS, which is what they configured; the setting is not silently
    applied and not silently dropped into an error either.
    """
    if not url.startswith("rediss://"):
        return {}
    if not connection.verify_ssl:
        return {"ssl_cert_reqs": "none"}
    if connection.ca_file_path:
        return {"ssl_ca_certs": connection.ca_file_path}
    return {}


def _redacted(url):
    """The URL with any userinfo removed, for a log line.

    The supported way to hold a Redis credential is an ExternalIntegration and its secrets group,
    but the plain `url` setting is documented for lab use and `redis://user:password@host` is a
    legal thing to put in it. A log line is a place a password must never reach, whichever way it
    arrived.
    """
    split = urlsplit(url)
    if not split.username and not split.password:
        return url
    host = split.hostname or ""
    if split.port:
        host = f"{host}:{split.port}"
    return urlunsplit((split.scheme, host, split.path, split.query, split.fragment))


class RedisEventConsumer(EventConsumer):
    """Redis pub/sub, where channels are topics and nothing is acknowledged."""

    supports_replay = False
    settings_key = "redis"

    def __init__(self, *, settings, topics):
        """Hold the settings; the client is built in `connect()`."""
        super().__init__(settings=settings, topics=topics)
        self._client = None
        self._pubsub = None

    def connect(self):
        """Subscribe to the configured channels.

        Pattern subscriptions are deliberately not offered: a message's channel is the key into
        per-topic configuration, and a pattern would leave that lookup ambiguous.
        """
        import redis  # pylint: disable=import-outside-toplevel

        connection = connection_details(self.settings, url_key="url")
        url = connection.url
        if not url:
            raise ImproperlyConfigured("The Redis consumer needs a url, or an external integration naming one.")

        options = {
            key: value for key, value in (("username", connection.username), ("password", connection.password)) if value
        }
        options.update(_tls_options(url, connection))
        self._client = redis.Redis.from_url(url, **options)
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(*self.topics)
        logger.info("Subscribed to %s on %s", ", ".join(self.topics), _redacted(url))

    def poll(self, timeout):
        """Return the next message, or None on a timeout."""
        raw = self._pubsub.get_message(timeout=timeout)
        if raw is None or raw.get("type") != "message":
            return None
        channel = raw["channel"]
        return BrokerMessage(
            topic=channel.decode("utf-8", "replace") if isinstance(channel, bytes) else str(channel),
            value=raw["data"],
        )

    def acknowledge(self, message):
        """Nothing to acknowledge to.

        Kept rather than raising, so that the loop has one shape for both brokers. The consequence
        is the one ADR 0004 names: with pub/sub, at-least-once is as good as the broker gets, and
        every instance receives every message, so running two duplicates the work. Rule S5 makes
        that harmless rather than correct.
        """

    def close(self):
        """Unsubscribe and drop the connection."""
        if self._pubsub is not None:
            self._pubsub.close()
            self._pubsub = None
        if self._client is not None:
            self._client.close()
            self._client = None
