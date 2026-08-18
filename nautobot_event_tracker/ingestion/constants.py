"""Vocabulary shared across the ingestion package.

These are configuration values and counter keys rather than model field choices, so they are plain
constants here instead of a `ChoiceSet` in `choices.py`. Keeping them in a module that imports
nothing from the package lets the configuration and the pre-filter both use them without a cycle.
"""

#: What the pre-filter decided to do with a message.
ACTION_ACCEPT = "accept"
ACTION_SUPPRESS = "suppress"
ACTION_DROP = "drop"

#: The one action only triage can reach: add this event to an existing open ticket (spec 6.3).
ACTION_ATTACH = "attach"

#: The actions an operator may ask for in a match rule. Accepting is what happens when no rule
#: fires, so it is not something a rule can ask for.
RULE_ACTIONS = (ACTION_DROP, ACTION_SUPPRESS)

#: The whole vocabulary a triage answer may use (T3). Anything else is read as accept.
TRIAGE_ACTIONS = (ACTION_ACCEPT, ACTION_ATTACH, ACTION_SUPPRESS, ACTION_DROP)

#: What to do with an event naming an event type the catalogue does not hold.
UNKNOWN_EVENT_TYPE_DEFAULT = "default"
UNKNOWN_EVENT_TYPE_DROP = "drop"
UNKNOWN_EVENT_TYPE_POLICIES = (UNKNOWN_EVENT_TYPE_DEFAULT, UNKNOWN_EVENT_TYPE_DROP)

#: Reasons the pre-filter refuses a message, used as `IngestionStats.drops_by_reason` keys. A match
#: rule contributes its own name instead, which is why rule names have to be unique within a topic.
REASON_UNKNOWN_TOPIC = "unknown_topic"
REASON_UNKNOWN_EVENT_TYPE = "unknown_event_type"
REASON_EVENT_TYPE_DISABLED = "event_type_disabled"
REASON_BELOW_SEVERITY_FLOOR = "below_severity_floor"
REASON_RATE_LIMITED = "rate_limited"

#: The fixed counter key for a triage drop (T8). The model's free-text reason goes into logs and
#: ticket messages, never into counter keys, which must stay a bounded set.
REASON_TRIAGE = "llm_triage"

#: Reasons a message never reached the pre-filter at all.
REASON_UNDECODABLE = "undecodable"
REASON_NOT_AN_OBJECT = "not_an_object"

#: The key under which a payload too large to store keeps its size.
PAYLOAD_TRUNCATED_KEY = "_truncated"
