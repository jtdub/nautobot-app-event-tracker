"""Test normalization: the payload shapes a real collector sends, and what each becomes.

Normalization is pure, so these are plain unit tests with no database. That is the point of
keeping it pure - the table below is cheap to extend when a device turns out to send something
nobody expected.
"""

import json
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.test import SimpleTestCase

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion import normalize
from nautobot_event_tracker.ingestion.config import TopicConfig
from nautobot_event_tracker.ingestion.constants import (
    PAYLOAD_TRUNCATED_KEY,
    REASON_NOT_AN_OBJECT,
    REASON_UNDECODABLE,
    UNKNOWN_EVENT_TYPE_DEFAULT,
)

MAX_PAYLOAD_BYTES = 65536


def topic(**overrides):
    """Build a topic configuration for these tests."""
    defaults = {
        "name": "network.events",
        "field_map": {
            "event_type": "event.type",
            "title": "message",
            "severity": "event.severity",
            "description": "detail",
            "occurred_at": "timestamp",
        },
        "defaults": {"event_type": "Unclassified", "severity": SeverityChoices.MINOR},
        "severity_map": {"3": SeverityChoices.MAJOR, "critical": SeverityChoices.CRITICAL},
        "dedup_key_template": "{event.type}:{host}",
        "minimum_severity": "",
        "unknown_event_type": UNKNOWN_EVENT_TYPE_DEFAULT,
    }
    return TopicConfig(**{**defaults, **overrides})


def payload(**overrides):
    """A well-formed payload, of the shape the lab's syslog bridge produces."""
    base = {
        "event": {"type": "Interface Down", "severity": "3"},
        "message": "Interface ethernet-1/1 is down",
        "detail": "admin-state disable",
        "host": "leaf-01",
        "timestamp": "2026-08-15T03:14:00Z",
    }
    base.update(overrides)
    return base


class TestDecode(SimpleTestCase):
    """A message body has to be a JSON object before it can be anything else."""

    def test_an_object_decodes(self):
        """The ordinary case."""
        self.assertEqual(normalize.decode(b'{"a": 1}'), {"a": 1})

    def test_invalid_json_carries_its_reason(self):
        """The reason is the counter key the message is recorded under."""
        with self.assertRaises(normalize.NormalizationError) as caught:
            normalize.decode(b"{not json")
        self.assertEqual(caught.exception.reason, REASON_UNDECODABLE)

    def test_a_json_list_is_not_an_object(self):
        """A batch of events is a shape this pipeline does not handle, and says so."""
        with self.assertRaises(normalize.NormalizationError) as caught:
            normalize.decode(b'[{"a": 1}]')
        self.assertEqual(caught.exception.reason, REASON_NOT_AN_OBJECT)

    def test_a_bare_scalar_is_not_an_object(self):
        """Neither is a plain string, which is what an unparsed syslog line would arrive as."""
        with self.assertRaises(normalize.NormalizationError) as caught:
            normalize.decode(b'"interface down"')
        self.assertEqual(caught.exception.reason, REASON_NOT_AN_OBJECT)


class TestResolvePath(SimpleTestCase):
    """Dotted paths, including the ways they fail."""

    def test_a_nested_path_resolves(self):
        """The ordinary case."""
        self.assertEqual(normalize.resolve_path(payload(), "event.type"), "Interface Down")

    def test_a_top_level_path_resolves(self):
        """A path with no dots is still a path."""
        self.assertEqual(normalize.resolve_path(payload(), "host"), "leaf-01")

    def test_a_missing_key_is_none(self):
        """Missing, not an exception: half a payload is normal."""
        self.assertIsNone(normalize.resolve_path(payload(), "event.nothing"))

    def test_a_non_object_intermediate_is_none(self):
        """`event` being a string must not raise on the way through it."""
        self.assertIsNone(normalize.resolve_path({"event": "a string"}, "event.type"))

    def test_a_null_value_is_none(self):
        """An explicit JSON null reads as absent, which is what a collector means by it."""
        self.assertIsNone(normalize.resolve_path({"event": {"type": None}}, "event.type"))


class TestRenderTemplate(SimpleTestCase):
    """The dedup key template, which is not `str.format` and must not behave like it."""

    def test_paths_are_substituted(self):
        """`{event.type}` is a path, not attribute access."""
        self.assertEqual(normalize.render_template("{event.type}:{host}", payload()), "Interface Down:leaf-01")

    def test_literal_text_is_kept(self):
        """Everything outside the braces survives."""
        self.assertEqual(normalize.render_template("evt/{host}/down", payload()), "evt/leaf-01/down")

    def test_a_missing_path_yields_an_empty_key(self):
        """Better one ticket per event than every event joined under one accidental key."""
        self.assertEqual(normalize.render_template("{event.type}:{missing}", payload()), "")

    def test_an_unclosed_brace_yields_an_empty_key(self):
        """A malformed template must not produce a key that half-works."""
        self.assertEqual(normalize.render_template("{event.type", payload()), "")

    def test_an_empty_template_yields_an_empty_key(self):
        """No template configured means dedup is off, which rule S5 already handles."""
        self.assertEqual(normalize.render_template("", payload()), "")

    def test_a_numeric_value_renders(self):
        """Keys are strings; the payload's types are not our problem."""
        self.assertEqual(normalize.render_template("{port}", {"port": 49152}), "49152")


class TestNormalize(SimpleTestCase):
    """A decoded payload becomes an event."""

    def normalized(self, data=None, topic_config=None, **kwargs):
        """Normalize with this test's defaults."""
        return normalize.normalize(
            data if data is not None else payload(),
            topic_config=topic_config or topic(),
            max_payload_bytes=MAX_PAYLOAD_BYTES,
            **kwargs,
        )

    def test_the_ordinary_payload(self):
        """Every mapped field lands where it should."""
        event = self.normalized()
        self.assertEqual(event.event_type_name, "Interface Down")
        self.assertEqual(event.title, "Interface ethernet-1/1 is down")
        self.assertEqual(event.description, "admin-state disable")
        self.assertEqual(event.severity, SeverityChoices.MAJOR)
        self.assertEqual(event.dedup_key, "Interface Down:leaf-01")
        self.assertEqual(event.topic, "network.events")

    def test_a_missing_field_falls_back_to_the_default(self):
        """Collectors omit fields; the configuration says what to do about it."""
        data = payload()
        del data["event"]["type"]
        self.assertEqual(self.normalized(data).event_type_name, "Unclassified")

    def test_a_missing_title_falls_back_to_the_event_type(self):
        """A ticket titled with an empty string is worse than a repetitive one."""
        data = payload()
        del data["message"]
        self.assertEqual(self.normalized(data).title, "Interface Down")

    def test_a_missing_description_is_empty_not_the_word_none(self):
        """`str(None)` on a ticket is the kind of thing nobody notices until a customer does."""
        data = payload()
        del data["detail"]
        self.assertEqual(self.normalized(data).description, "")

    def test_a_numeric_severity_maps_the_same_as_a_string_one(self):
        """Syslog severities arrive as both, sometimes in the same stream."""
        as_number = self.normalized(payload(event={"type": "Interface Down", "severity": 3}))
        as_string = self.normalized(payload(event={"type": "Interface Down", "severity": "3"}))
        self.assertEqual(as_number.severity, SeverityChoices.MAJOR)
        self.assertEqual(as_string.severity, SeverityChoices.MAJOR)

    def test_a_severity_already_on_our_scale_passes_through(self):
        """A collector that speaks our vocabulary should not need a map entry."""
        data = payload(event={"type": "Interface Down", "severity": SeverityChoices.CRITICAL})
        self.assertEqual(self.normalized(data).severity, SeverityChoices.CRITICAL)

    def test_an_unmapped_severity_falls_back_to_the_default(self):
        """An unknown value must not reach the database as a severity."""
        data = payload(event={"type": "Interface Down", "severity": "purple"})
        self.assertEqual(self.normalized(data).severity, SeverityChoices.MINOR)

    def test_no_severity_anywhere_leaves_it_for_the_event_type(self):
        """An empty severity means the event type's default settles it."""
        config = topic(defaults={"event_type": "Unclassified"})
        data = payload(event={"type": "Interface Down"})
        self.assertEqual(self.normalized(data, config).severity, "")

    def test_an_iso_timestamp_is_parsed(self):
        """The ordinary case, and the one the lab produces."""
        self.assertEqual(
            self.normalized().occurred_at,
            datetime(2026, 8, 15, 3, 14, tzinfo=datetime_timezone.utc),
        )

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Guessing the local timezone of a device log is worse than assuming UTC and saying so."""
        data = payload(timestamp="2026-08-15T03:14:00")
        self.assertEqual(self.normalized(data).occurred_at.tzinfo, datetime_timezone.utc)

    def test_an_epoch_timestamp_is_parsed(self):
        """Collectors send epoch seconds at least as often as ISO strings."""
        data = payload(timestamp=1786763640)
        self.assertEqual(self.normalized(data).occurred_at, datetime(2026, 8, 15, 3, 14, tzinfo=datetime_timezone.utc))

    def test_an_unparseable_timestamp_falls_back_to_the_broker(self):
        """The broker's own timestamp is a better answer than now."""
        broker_time = datetime(2026, 1, 1, tzinfo=datetime_timezone.utc)
        data = payload(timestamp="last tuesday")
        self.assertEqual(self.normalized(data, broker_timestamp=broker_time).occurred_at, broker_time)

    def test_an_absent_timestamp_with_no_broker_time_falls_back_to_now(self):
        """Something has to be first_seen, and it must be aware."""
        data = payload()
        del data["timestamp"]
        self.assertIsNotNone(self.normalized(data).occurred_at.tzinfo)

    def test_the_payload_is_kept_whole(self):
        """The raw event is the evidence; Phase 4's resolver reads it."""
        self.assertEqual(self.normalized().payload, payload())

    def test_an_oversize_payload_is_replaced_by_its_size_and_a_preview(self):
        """A 4 MB telemetry frame must not become a 4 MB row."""
        data = payload(bulk="x" * 200)
        event = normalize.normalize(data, topic_config=topic(), max_payload_bytes=100)
        self.assertTrue(event.payload[PAYLOAD_TRUNCATED_KEY])
        self.assertGreater(event.payload["_size_bytes"], 100)
        self.assertIn("Interface Down", event.payload["_preview"])

    def test_a_payload_at_the_cap_is_kept(self):
        """The cap is a limit, not a target to stay under."""
        data = {"a": "b"}
        event = normalize.normalize(data, topic_config=topic(), max_payload_bytes=len(json.dumps(data)))
        self.assertEqual(event.payload, data)
