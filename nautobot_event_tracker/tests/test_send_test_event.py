"""Test `development/send_test_event.py`, the script behind `invoke send-test-event`."""

import json
import os
import sys
import types
from unittest import mock

from django.test import SimpleTestCase, TestCase
from nautobot.apps.choices import SecretsGroupAccessTypeChoices, SecretsGroupSecretTypeChoices
from nautobot.extras.models import ExternalIntegration, Secret, SecretsGroup, SecretsGroupAssociation

from nautobot_event_tracker.ingestion.consumers import KafkaEventConsumer
from nautobot_event_tracker.ingestion.consumers import kafka as kafka_module
from nautobot_event_tracker.tests.test_ingestion_consumers import StubKafkaConsumer
from nautobot_event_tracker.tests.test_lab_configuration import DEVELOPMENT, from_development, in_a_source_checkout

SCRIPT_PATH = DEVELOPMENT / "send_test_event.py"
TOPIC = "network.events"
CA_PATH = "/etc/ssl/private-ca.pem"
USERNAME_VARIABLE = "EVENT_TRACKER_TEST_BROKER_USERNAME"
PASSWORD_VARIABLE = "EVENT_TRACKER_TEST_BROKER_PASSWORD"


class StubProducer:
    """A confluent-kafka Producer that records each call that the script makes."""

    def __init__(self, config):
        """Keep the configuration so the tests can assert on it."""
        self.config = config
        self.produced = []
        self.flush_timeouts = []

    def produce(self, topic, value):
        """Record the message."""
        self.produced.append((topic, value))

    def flush(self, timeout):
        """Record the timeout."""
        self.flush_timeouts.append(timeout)


def loaded_with(**consumer_settings):
    """Return the part of the loaded configuration that the publish functions read."""
    return types.SimpleNamespace(consumer_settings=consumer_settings)


@in_a_source_checkout
class TestThePayload(SimpleTestCase):
    """The message the script builds."""

    def setUp(self):
        """Load the script."""
        self.script = from_development("send_test_event", SCRIPT_PATH)

    def test_the_interface_option_reaches_the_payload(self):
        """The `--interface` value is the `interface` field and is in the message text."""
        arguments = self.script.parse(["--interface", "ethernet-1/2"])
        payload = json.loads(self.script._payload(arguments, 0))  # pylint: disable=protected-access
        self.assertEqual(payload["interface"], "ethernet-1/2")
        self.assertEqual(payload["message"], "Interface ethernet-1/2 is down")


@in_a_source_checkout
class TestThePublishToRedis(TestCase):
    """What the script gives redis-py."""

    def setUp(self):
        """Load the script."""
        self.script = from_development("send_test_event", SCRIPT_PATH)

    def publish(self, payloads, **consumer_settings):
        """Run the Redis path and return the substitute client and the `from_url` call."""
        client = mock.Mock()
        with mock.patch("redis.Redis.from_url", return_value=client) as from_url:
            self.script._publish_to_redis(  # pylint: disable=protected-access
                loaded_with(**consumer_settings), TOPIC, payloads
            )
        return client, from_url

    def integration(self, **fields):
        """Create the External Integration `tls-redis`."""
        ExternalIntegration.objects.create(name="tls-redis", **fields)
        return {"external_integration": "tls-redis"}

    def test_a_plain_url_publishes_each_payload(self):
        """A plain URL in the settings gives no keyword, and each payload is published."""
        client, from_url = self.publish(["one", "two"], url="redis://redis:6379/2", external_integration="")
        from_url.assert_called_once_with("redis://redis:6379/2")
        self.assertEqual(client.publish.call_args_list, [mock.call(TOPIC, "one"), mock.call(TOPIC, "two")])

    def test_a_ca_path_reaches_the_client(self):
        """The CA path of the integration becomes `ssl_ca_certs`."""
        settings = self.integration(remote_url="rediss://broker:6379/0", ca_file_path=CA_PATH)
        _, from_url = self.publish(["one"], **settings)
        from_url.assert_called_once_with("rediss://broker:6379/0", ssl_ca_certs=CA_PATH)

    def test_unticked_verify_ssl_reaches_the_client(self):
        """An integration with Verify SSL off gives `ssl_cert_reqs="none"`."""
        settings = self.integration(remote_url="rediss://broker:6379/0", ca_file_path=CA_PATH, verify_ssl=False)
        _, from_url = self.publish(["one"], **settings)
        from_url.assert_called_once_with("rediss://broker:6379/0", ssl_cert_reqs="none")

    def test_a_plaintext_url_gets_no_tls_keyword(self):
        """redis-py refuses an SSL keyword on a `redis://` URL."""
        settings = self.integration(remote_url="redis://broker:6379/0", ca_file_path=CA_PATH)
        _, from_url = self.publish(["one"], **settings)
        from_url.assert_called_once_with("redis://broker:6379/0")


@in_a_source_checkout
class TestThePublishToKafka(TestCase):
    """What the script gives the Kafka producer."""

    def setUp(self):
        """Load the script."""
        self.script = from_development("send_test_event", SCRIPT_PATH)

    def publish(self, payloads, **consumer_settings):
        """Run the Kafka path and return the substitute producer."""
        holder = {}

        def producer_class(config):
            """Build the stub and remember it."""
            holder["stub"] = StubProducer(config)
            return holder["stub"]

        with mock.patch.dict(sys.modules, {"confluent_kafka": types.SimpleNamespace(Producer=producer_class)}):
            self.script._publish_to_kafka(  # pylint: disable=protected-access
                loaded_with(**consumer_settings), TOPIC, payloads
            )
        return holder["stub"]

    def integration(self, **fields):
        """Create the External Integration `tls-kafka`."""
        return ExternalIntegration.objects.create(name="tls-kafka", remote_url="kafka://broker:9092", **fields)

    def with_credentials(self, integration):
        """Give the integration a secrets group with a username and a password."""
        group = SecretsGroup.objects.create(name="tls-kafka credentials")
        for secret_type, variable in (
            (SecretsGroupSecretTypeChoices.TYPE_USERNAME, USERNAME_VARIABLE),
            (SecretsGroupSecretTypeChoices.TYPE_PASSWORD, PASSWORD_VARIABLE),
        ):
            secret = Secret.objects.create(
                name=f"tls-kafka {secret_type}", provider="environment-variable", parameters={"variable": variable}
            )
            SecretsGroupAssociation.objects.create(
                secrets_group=group,
                secret=secret,
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=secret_type,
            )
        integration.secrets_group = group
        integration.save()
        patcher = mock.patch.dict(os.environ, {USERNAME_VARIABLE: "event-user", PASSWORD_VARIABLE: "event-pass"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plain_bootstrap_servers_produce_each_payload(self):
        """A plain server list gives only `bootstrap.servers`, and each payload is produced."""
        stub = self.publish(["one", "two"], bootstrap_servers=["redpanda:9092"], external_integration="")
        self.assertEqual(stub.config, {"bootstrap.servers": "redpanda:9092"})
        self.assertEqual(stub.produced, [(TOPIC, b"one"), (TOPIC, b"two")])
        self.assertEqual(stub.flush_timeouts, [10])

    def test_a_ca_path_reaches_the_producer(self):
        """The CA path of the integration becomes `ssl.ca.location`."""
        self.integration(ca_file_path=CA_PATH)
        stub = self.publish(["one"], external_integration="tls-kafka")
        self.assertEqual(stub.config, {"bootstrap.servers": "kafka://broker:9092", "ssl.ca.location": CA_PATH})

    def test_unticked_verify_ssl_reaches_the_producer(self):
        """Verify SSL off turns off the certificate check and drops the CA path."""
        self.integration(ca_file_path=CA_PATH, verify_ssl=False)
        stub = self.publish(["one"], external_integration="tls-kafka")
        self.assertEqual(
            stub.config,
            {"bootstrap.servers": "kafka://broker:9092", "enable.ssl.certificate.verification": False},
        )

    def credentials_settings(self):
        """Return the settings of the integration with credentials and a SASL_SSL protocol."""
        self.with_credentials(self.integration(ca_file_path=CA_PATH))
        return {
            "external_integration": "tls-kafka",
            "security_protocol": "SASL_SSL",
            "sasl_mechanism": "SCRAM-SHA-256",
        }

    def test_credentials_bring_the_security_settings(self):
        """A username and a password bring the protocol, the mechanism and the TLS key."""
        stub = self.publish(["one"], **self.credentials_settings())
        self.assertEqual(
            stub.config,
            {
                "bootstrap.servers": "kafka://broker:9092",
                "security.protocol": "SASL_SSL",
                "sasl.mechanism": "SCRAM-SHA-256",
                "sasl.username": "event-user",
                "sasl.password": "event-pass",
                "ssl.ca.location": CA_PATH,
            },
        )

    def test_the_connection_keys_match_the_consumer(self):
        """For the same settings, the script and the Kafka consumer set the same connection keys."""
        settings = {**self.credentials_settings(), "group_id": "test-group"}
        stub = self.publish(["one"], **settings)
        holder = {}

        def consumer_class(config):
            """Build the stub and remember it."""
            holder["stub"] = StubKafkaConsumer(config)
            return holder["stub"]

        consumer = KafkaEventConsumer(settings=settings, topics=(TOPIC,))
        with mock.patch.object(kafka_module, "_import_consumer", return_value=consumer_class):
            consumer.connect()
        consumer_keys = {
            key: value
            for key, value in holder["stub"].config.items()
            if key not in ("group.id", "enable.auto.commit", "auto.offset.reset")
        }
        self.assertEqual(stub.config, consumer_keys)
