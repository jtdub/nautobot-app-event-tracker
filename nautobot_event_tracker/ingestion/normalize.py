"""Turning a broker message into the typed event the rest of the pipeline handles.

Normalization is pure: configuration and a message in, a value out. It touches no database and
reads the clock only where a timestamp is missing entirely, which is what makes the table of
payload shapes in the tests cheap to write.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.utils import dateparse, timezone

from nautobot_event_tracker.choices import SeverityChoices
from nautobot_event_tracker.ingestion.constants import (
    PAYLOAD_TRUNCATED_KEY,
    REASON_NOT_AN_OBJECT,
    REASON_UNDECODABLE,
)

#: How much of an oversize payload to keep, so that a person can still see what arrived.
PREVIEW_CHARACTERS = 1024


class NormalizationError(Exception):
    """A message that cannot become an event. Carries the counter key it should be recorded under."""

    def __init__(self, reason, message=""):
        """Record the reason alongside the text."""
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class NormalizedEvent:  # pylint: disable=too-many-instance-attributes
    """One event, in the shape `services.tickets.create_ticket()` wants it.

    `severity` may be empty, meaning the payload did not say and the configuration had no default.
    The event type's own default settles it - in the service on the write path, and through
    `effective_severity()` where the pre-filter needs to compare before writing anything.
    """

    topic: str
    event_type_name: str
    title: str
    severity: str
    description: str
    dedup_key: str
    occurred_at: datetime
    payload: dict


def decode(value):
    """Decode a message body into a JSON object, or raise `NormalizationError`."""
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise NormalizationError(REASON_UNDECODABLE, str(error)) from error
    if not isinstance(decoded, dict):
        raise NormalizationError(REASON_NOT_AN_OBJECT, f"payload is {type(decoded).__name__}, not an object")
    return decoded


def resolve_path(payload, path):
    """Walk a dotted path into nested objects, returning None rather than raising.

    A missing key, a non-object where an object was expected, and an empty path all yield None. A
    key containing a literal dot is not addressable, which is a limitation worth knowing and not
    worth an escaping syntax.
    """
    current = payload
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def render_template(template, payload):
    """Render a `{path}` template over the payload.

    Deliberately not `str.format`: under it `{event.type}` means attribute access on an argument
    named `event`, which is not what an operator reading this configuration expects.

    Returns an empty string when any path is missing, so that an unresolvable template gives every
    event its own ticket rather than joining them all under one accidental key.
    """
    if not template:
        return ""

    out = []
    rest = template
    while "{" in rest:
        before, _, after = rest.partition("{")
        path, closed, rest = after.partition("}")
        if not closed:
            return ""
        value = resolve_path(payload, path)
        if value is None:
            return ""
        out.append(before)
        out.append(str(value))
    out.append(rest)
    return "".join(out)


def normalize(payload, *, topic_config, broker_timestamp=None):
    """Build a `NormalizedEvent` from a decoded payload and its topic's configuration."""
    field_map = topic_config.field_map
    defaults = topic_config.defaults

    def mapped(key):
        """The payload's value for a mapped field, falling back to the configured default."""
        value = resolve_path(payload, field_map[key]) if field_map.get(key) else None
        return value if value is not None else defaults.get(key)

    event_type_name = _as_text(mapped("event_type"))
    title = _as_text(mapped("title")) or event_type_name

    return NormalizedEvent(
        topic=topic_config.name,
        event_type_name=event_type_name,
        title=title,
        severity=_severity(mapped("severity"), topic_config),
        description=_as_text(mapped("description")),
        dedup_key=render_template(topic_config.dedup_key_template, payload),
        occurred_at=_occurred_at(mapped("occurred_at"), broker_timestamp),
        payload=payload,
    )


def _severity(raw, topic_config):
    """Map a raw severity onto the app's own scale.

    Lookup is by string, so a syslog severity arriving as `4` and one arriving as `"4"` behave the
    same - collectors send both, sometimes in the same stream.
    """
    if raw is not None:
        mapped = topic_config.severity_map.get(str(raw))
        if mapped:
            return mapped
        if str(raw) in SeverityChoices.values():
            return str(raw)
    default = topic_config.defaults.get("severity")
    return default if default in SeverityChoices.values() else ""


def _occurred_at(raw, broker_timestamp):
    """When the event happened: the payload's word for it, then the broker's, then now."""
    parsed = _parse_timestamp(raw)
    if parsed is not None:
        return parsed
    if broker_timestamp is not None:
        return broker_timestamp
    return timezone.now()


def _parse_timestamp(raw):
    """Parse an ISO 8601 string or an epoch number. Naive values are read as UTC."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=datetime_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = dateparse.parse_datetime(str(raw))
    except ValueError:
        return None
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, datetime_timezone.utc)


def capped(payload, max_payload_bytes):
    """Keep the payload, unless it is large enough to be a problem of its own.

    An uncapped JSONField fed by streaming telemetry is a table that grows in a way nobody planned
    for. Over the cap, the ticket keeps the size and a readable prefix instead of the whole frame.

    Applied on the write path rather than during normalization: serializing a payload to measure it
    is the most expensive thing in the per-message path, and most messages in a filtered stream
    never become a ticket. It also means a match rule sees the event the device sent rather than a
    truncation marker.
    """
    encoded = json.dumps(payload)
    size = len(encoded.encode("utf-8"))
    if size <= max_payload_bytes:
        return payload
    return {
        PAYLOAD_TRUNCATED_KEY: True,
        "_size_bytes": size,
        "_preview": encoded[:PREVIEW_CHARACTERS],
    }


def _as_text(value):
    """Render a mapped value as text, treating a missing one as empty rather than as 'None'."""
    return "" if value is None else str(value)
