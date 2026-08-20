"""Test the broker layer.

The conformance suite runs against every implementation, including the test fake, so that the fake
cannot drift into being easier to satisfy than the real thing. Kafka is exercised against a stub
client rather than a broker: what is worth testing here is that offsets are committed when the
pipeline says so, which a broker would not make any clearer.
"""

from datetime import datetime, timezone
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from nautobot_event_tracker.ingestion.consumers import (
    CONSUMERS,
    BrokerMessage,
    EventConsumer,
    KafkaEventConsumer,
    RedisEventConsumer,
    connection_details,
    get_consumer_class,
)
from nautobot_event_tracker.ingestion.consumers import kafka as kafka_module
from nautobot_event_tracker.ingestion.consumers.base import BrokerConnection
from nautobot_event_tracker.ingestion.consumers.redis import _redacted, _tls_options
from nautobot_event_tracker.tests import fixtures


class StubKafkaMessage:
    """What confluent-kafka hands back from `poll()`."""

    def __init__(  # pylint: disable=too-many-arguments
        self, *, value=b"{}", topic="network.events", partition=0, offset=7, key=None, timestamp=(1, 1786763640000)
    ):
        """Record what the stub should report."""
        self._value = value
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._key = key
        self._timestamp = timestamp
        self._error = None

    def value(self):
        """The message body."""
        return self._value

    def topic(self):
        """The topic it arrived on."""
        return self._topic

    def partition(self):
        """The partition it arrived on, which is what its offset counts within."""
        return self._partition

    def offset(self):
        """Its offset."""
        return self._offset

    def key(self):
        """Its key, which this app does not use but does carry."""
        return self._key

    def timestamp(self):
        """A (kind, milliseconds) pair, as the real client returns."""
        return self._timestamp

    def error(self):
        """None for a real message; set for a partition event."""
        return self._error


class StubKafkaConsumer:  # pylint: disable=unused-argument
    """A confluent-kafka Consumer that records what was asked of it.

    Signatures mirror the real client's, argument names included: `commit(asynchronous=False)` is
    called by keyword, so the name is part of the contract even where the stub ignores the value.
    """

    def __init__(self, config):
        """Keep the configuration so the tests can assert on it."""
        self.config = config
        self.subscribed = None
        self.commits = 0
        self.closed = False
        self.messages = []

    def subscribe(self, topics):
        """Record the subscription."""
        self.subscribed = topics

    def poll(self, timeout):
        """Hand over the next queued message."""
        return self.messages.pop(0) if self.messages else None

    def commit(self, asynchronous=True):
        """Count the commit, which is what `acknowledge()` is for."""
        self.commits += 1

    def close(self):
        """Note that we left the group."""
        self.closed = True


class TestConsumerRegistry(SimpleTestCase):
    """Choosing an implementation by name."""

    def test_every_name_resolves_to_a_consumer(self):
        """A name in the settings has to reach a class that satisfies the interface."""
        for name in CONSUMERS:
            self.assertTrue(issubclass(get_consumer_class(name), EventConsumer), name)

    def test_an_unknown_name_lists_the_ones_that_exist(self):
        """The error an operator sees should answer the question it raises."""
        with self.assertRaises(ImproperlyConfigured) as caught:
            get_consumer_class("rabbitmq")
        self.assertIn("kafka", str(caught.exception))
        self.assertIn("redis", str(caught.exception))

    def test_the_kafka_class_imports_without_its_extra(self):
        """The module must load on a deployment that never installed the client."""
        self.assertTrue(issubclass(KafkaEventConsumer, EventConsumer))


class TestMessageIdentity(SimpleTestCase):
    """What names one message, for the step (T5) that must not pay for it twice."""

    @staticmethod
    def message(**kwargs):
        """A message carrying whatever the broker would have filled in."""
        return BrokerMessage(topic="network.events", value=b'{"host": "leaf-01"}', **kwargs)

    def test_the_same_message_has_the_same_identity(self):
        """Two reads of one message are one message."""
        self.assertEqual(self.message(partition=0, offset=7).identity, self.message(partition=0, offset=7).identity)

    def test_two_partitions_at_one_offset_are_two_messages(self):
        """Kafka numbers offsets per partition, so an offset alone is not an identity."""
        self.assertNotEqual(self.message(partition=0, offset=7).identity, self.message(partition=1, offset=7).identity)

    def test_a_broker_without_offsets_falls_back_to_the_payload(self):
        """Redis pub/sub numbers nothing, so identity comes from what arrived."""
        self.assertEqual(self.message().identity, self.message().identity)
        other = BrokerMessage(topic="network.events", value=b'{"host": "leaf-02"}')
        self.assertNotEqual(self.message().identity, other.identity)

    def test_one_payload_on_two_topics_is_two_messages(self):
        """The topic decides which configuration judged it, so it is part of the identity."""
        other = BrokerMessage(topic="other.events", value=b'{"host": "leaf-01"}')
        self.assertNotEqual(self.message().identity, other.identity)


class ConsumerConformanceTests:  # pylint: disable=no-member
    """The contract every implementation keeps. Mixed into one test case per implementation.

    A mixin rather than a base class, so each implementation's own tests sit alongside it. The
    assertion methods come from the TestCase it is mixed into.
    """

    def build(self):
        """Return a connected consumer and whatever the test needs to drive it."""
        raise NotImplementedError

    def test_poll_returns_none_when_nothing_arrives(self):
        """Returning on a timeout is what lets the loop notice a shutdown signal."""
        consumer, _ = self.build()
        self.assertIsNone(consumer.poll(0.01))

    def test_poll_returns_a_broker_message(self):
        """Every implementation hands back the same shape."""
        consumer, deliver = self.build()
        deliver(b'{"a": 1}')
        message = consumer.poll(0.01)
        self.assertIsInstance(message, BrokerMessage)
        self.assertEqual(message.value, b'{"a": 1}')
        self.assertEqual(message.topic, "network.events")

    def test_acknowledge_accepts_a_message(self):
        """Even where there is nothing to acknowledge to, the loop has one shape."""
        consumer, deliver = self.build()
        deliver(b"{}")
        consumer.acknowledge(consumer.poll(0.01))

    def test_close_is_safe(self):
        """The loop closes on every path out, including the ones that never connected properly."""
        consumer, _ = self.build()
        consumer.close()
        consumer.close()


class TestFakeConsumer(ConsumerConformanceTests, SimpleTestCase):
    """The in-memory consumer the rest of the suite runs on."""

    def test_it_does_not_claim_to_replay(self):
        """It holds a list; there is nothing to resume from."""
        self.assertFalse(fixtures.FakeEventConsumer.supports_replay)

    def build(self):
        """A fake with a way to push messages into it."""
        consumer = fixtures.FakeEventConsumer(topics=("network.events",))
        consumer.connect()
        return consumer, lambda value: consumer.messages.append(BrokerMessage(topic="network.events", value=value))


class TestKafkaConsumer(ConsumerConformanceTests, SimpleTestCase):
    """Kafka, against a stub client."""

    def build(self):
        """A Kafka consumer wired to a stub, with a way to queue raw messages."""
        stub_holder = {}

        def consumer_class(config):
            """Build the stub and remember it."""
            stub_holder["stub"] = StubKafkaConsumer(config)
            return stub_holder["stub"]

        consumer = KafkaEventConsumer(
            settings={"bootstrap_servers": ["broker:9092"], "group_id": "test-group", "external_integration": ""},
            topics=("network.events",),
        )
        with mock.patch.object(kafka_module, "_import_consumer", return_value=consumer_class):
            consumer.connect()
        return consumer, lambda value: stub_holder["stub"].messages.append(StubKafkaMessage(value=value))

    def test_it_declares_that_it_can_replay(self):
        """Kafka commits offsets, so a consumer that dies resumes where it stopped (ADR 0004)."""
        self.assertTrue(KafkaEventConsumer.supports_replay)

    def test_auto_commit_is_off(self):
        """Auto-commit would move offsets on a timer, turning a crash into a loss."""
        consumer, _ = self.build()
        self.assertFalse(consumer._consumer.config["enable.auto.commit"])  # pylint: disable=protected-access

    def test_a_new_group_starts_at_the_beginning(self):
        """Events that arrived while nobody was consuming are the ones worth having."""
        consumer, _ = self.build()
        self.assertEqual(consumer._consumer.config["auto.offset.reset"], "earliest")  # pylint: disable=protected-access

    def test_it_subscribes_to_the_configured_topics(self):
        """A topic with no configuration would have nothing to normalize against."""
        consumer, _ = self.build()
        self.assertEqual(consumer._consumer.subscribed, ["network.events"])  # pylint: disable=protected-access

    def test_acknowledging_commits_synchronously(self):
        """The offset has to be committed before the loop moves on, not eventually."""
        consumer, deliver = self.build()
        deliver(b"{}")
        message = consumer.poll(0.01)
        consumer.acknowledge(message)
        self.assertEqual(consumer._consumer.commits, 1)  # pylint: disable=protected-access

    def test_polling_carries_the_offset_and_broker_timestamp(self):
        """Both appear in log lines about poison messages, and the timestamp is a fallback."""
        consumer, deliver = self.build()
        deliver(b"{}")
        message = consumer.poll(0.01)
        self.assertEqual(message.offset, 7)
        self.assertEqual(message.timestamp, datetime(2026, 8, 15, 3, 14, tzinfo=timezone.utc))

    def test_polling_carries_the_partition(self):
        """An offset counts within its partition, so one without the other names no message."""
        consumer, _ = self.build()
        consumer._consumer.messages.append(StubKafkaMessage(partition=3))  # pylint: disable=protected-access
        self.assertEqual(consumer.poll(0.01).partition, 3)

    def test_a_partition_event_is_not_a_message(self):
        """End-of-partition and rebalance notices are not events; the loop goes round again."""
        consumer, _ = self.build()
        raw = StubKafkaMessage()
        raw._error = "end of partition"  # pylint: disable=protected-access
        consumer._consumer.messages.append(raw)  # pylint: disable=protected-access
        self.assertIsNone(consumer.poll(0.01))

    def test_a_missing_client_explains_how_to_install_it(self):
        """The extra is optional, so this is the error most likely to be seen in the wild."""
        consumer = KafkaEventConsumer(settings={"bootstrap_servers": ["b:9092"], "group_id": "g"}, topics=("t",))
        with mock.patch.object(
            kafka_module, "_import_consumer", side_effect=ImproperlyConfigured(kafka_module.INSTALL_HINT)
        ):
            with self.assertRaises(ImproperlyConfigured) as caught:
                consumer.connect()
        self.assertIn("nautobot-event-tracker[kafka]", str(caught.exception))

    def test_no_servers_configured_is_refused(self):
        """Connecting to nothing in particular is not a state worth reaching."""
        consumer = KafkaEventConsumer(settings={"bootstrap_servers": [], "group_id": "g"}, topics=("t",))
        with mock.patch.object(kafka_module, "_import_consumer", return_value=StubKafkaConsumer):
            with self.assertRaises(ImproperlyConfigured):
                consumer.connect()


class TestRedisConsumer(ConsumerConformanceTests, SimpleTestCase):
    """Redis pub/sub, against a stub client."""

    def build(self):
        """A Redis consumer whose pubsub is a stub, with a way to queue raw messages."""
        queued = []

        class StubPubSub:
            """The subset of redis-py's PubSub this consumer uses."""

            def __init__(self):
                """Start unsubscribed."""
                self.subscribed = None
                self.closed = False

            def subscribe(self, *channels):
                """Record the subscription."""
                self.subscribed = channels

            def get_message(self, timeout=None):  # pylint: disable=unused-argument
                """Hand over the next queued message."""
                return queued.pop(0) if queued else None

            def close(self):
                """Note that we unsubscribed."""
                self.closed = True

        class StubClient:
            """The subset of redis-py's Redis this consumer uses."""

            def pubsub(self, **kwargs):
                """Return the stub pubsub."""
                return StubPubSub()

            def close(self):
                """Nothing to release."""

        consumer = RedisEventConsumer(
            settings={"url": "redis://localhost:6379/2", "external_integration": ""},
            topics=("network.events",),
        )
        with mock.patch("redis.Redis.from_url", return_value=StubClient()):
            consumer.connect()
        return consumer, lambda value: queued.append({"type": "message", "channel": b"network.events", "data": value})

    def test_it_does_not_claim_to_replay(self):
        """The documentation says messages published while it is down are gone; so does the code."""
        self.assertFalse(RedisEventConsumer.supports_replay)

    def test_a_subscription_confirmation_is_not_a_message(self):
        """Redis sends these on subscribe, and they are not events."""
        consumer, _ = self.build()
        consumer._pubsub.get_message = lambda timeout=None: {  # pylint: disable=protected-access
            "type": "subscribe",
            "channel": b"network.events",
            "data": 1,
        }
        self.assertIsNone(consumer.poll(0.01))

    def test_no_url_configured_is_refused(self):
        """A lab default is one thing; no address at all is a configuration error."""
        consumer = RedisEventConsumer(settings={"url": "", "external_integration": ""}, topics=("t",))
        with self.assertRaises(ImproperlyConfigured):
            consumer.connect()


class TestConnectionDetails(TestCase):
    """Where a broker's address and credentials come from."""

    def test_settings_are_used_when_no_integration_is_named(self):
        """The lab case: a plain URL in the settings file, and nothing to say about TLS."""
        connection = connection_details({"url": "redis://localhost:6379/0"}, url_key="url")
        self.assertEqual(connection.url, "redis://localhost:6379/0")
        self.assertIsNone(connection.username)
        self.assertIsNone(connection.password)
        self.assertTrue(connection.verify_ssl)
        self.assertEqual(connection.ca_file_path, "")

    def test_a_missing_integration_is_named_in_the_error(self):
        """Naming an object that does not exist should say which object."""
        with self.assertRaises(ImproperlyConfigured) as caught:
            connection_details({"external_integration": "no-such-integration"}, url_key="url")
        self.assertIn("no-such-integration", str(caught.exception))

    def test_an_integration_supplies_the_url(self):
        """Credentials belong where every other credential in the deployment is rotated."""
        from nautobot.extras.models import ExternalIntegration  # pylint: disable=import-outside-toplevel

        ExternalIntegration.objects.create(name="lab-broker", remote_url="kafka://broker:9092")
        connection = connection_details({"external_integration": "lab-broker"}, url_key="url")
        self.assertEqual(connection.url, "kafka://broker:9092")
        self.assertIsNone(connection.username)
        self.assertIsNone(connection.password)

    def test_the_integrations_tls_settings_come_back_with_it(self):
        """The three fields an operator sets expecting them to be obeyed."""
        from nautobot.extras.models import ExternalIntegration  # pylint: disable=import-outside-toplevel

        ExternalIntegration.objects.create(
            name="private-ca-broker",
            remote_url="rediss://broker:6379/0",
            verify_ssl=True,
            ca_file_path="/etc/ssl/private-ca.pem",
        )
        connection = connection_details({"external_integration": "private-ca-broker"}, url_key="url")
        self.assertTrue(connection.verify_ssl)
        self.assertEqual(connection.ca_file_path, "/etc/ssl/private-ca.pem")

    def test_a_templated_remote_url_is_rendered(self):
        """Nautobot supports Jinja2 here; reading the field raw hands the client a literal brace."""
        from nautobot.extras.models import ExternalIntegration  # pylint: disable=import-outside-toplevel

        ExternalIntegration.objects.create(name="templated", remote_url="redis://{{ obj.name }}:6379/0")
        connection = connection_details({"external_integration": "templated"}, url_key="url")
        self.assertEqual(connection.url, "redis://templated:6379/0")


class TestTheBrokerTlsOptions(SimpleTestCase):
    """What reaches redis-py, which refuses an SSL keyword on a plaintext connection."""

    def _options(self, url, **overrides):
        """The TLS keywords for this URL and integration settings."""
        return _tls_options(url, BrokerConnection(url=url, **overrides))

    def test_a_plaintext_url_takes_none_of_them(self):
        """`redis://` builds a Connection, which accepts no SSL keyword at all."""
        self.assertEqual(self._options("redis://broker:6379/0", ca_file_path="/etc/ssl/ca.pem"), {})

    def test_a_ca_path_is_passed_for_an_encrypted_url(self):
        """The private-CA case, which is the one that fails today with nothing explaining it."""
        self.assertEqual(
            self._options("rediss://broker:6379/0", ca_file_path="/etc/ssl/ca.pem"),
            {"ssl_ca_certs": "/etc/ssl/ca.pem"},
        )

    def test_unticking_verify_ssl_wins(self):
        """An operator who unticked it has said not to verify."""
        self.assertEqual(
            self._options("rediss://broker:6379/0", verify_ssl=False, ca_file_path="/etc/ssl/ca.pem"),
            {"ssl_cert_reqs": "none"},
        )


class TestTheLoggedUrl(SimpleTestCase):
    """A password in a lab URL is still a password, and a log line is still a log line."""

    def test_userinfo_is_stripped(self):
        """The one place a plain `url` setting can leak a credential."""
        self.assertEqual(
            _redacted("redis://someone:hunter2@broker.example.com:6379/0"),
            "redis://broker.example.com:6379/0",
        )

    def test_a_url_without_credentials_is_left_alone(self):
        """The ordinary case reads exactly as it did."""
        self.assertEqual(_redacted("redis://redis:6379/0"), "redis://redis:6379/0")
