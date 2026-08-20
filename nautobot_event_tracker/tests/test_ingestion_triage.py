"""Tests for LLM triage, against rules T1-T7 from the Phase 3 spec.

The fake sits at `TriageFilter`'s `complete` seam and routes through the real `services.llm`
with a canned client, so every triage decision here writes a real usage record - and no test
mocks litellm internals or opens a network connection.
"""

from django.test import TestCase
from django.utils import timezone

from nautobot_event_tracker.choices import TicketSourceChoices, TicketStatusChoices
from nautobot_event_tracker.ingestion import config
from nautobot_event_tracker.ingestion.constants import ACTION_ACCEPT, ACTION_ATTACH, ACTION_DROP, ACTION_SUPPRESS
from nautobot_event_tracker.ingestion.normalize import NormalizedEvent
from nautobot_event_tracker.ingestion.prefilter import ACCEPT, Decision, FilterResult
from nautobot_event_tracker.ingestion.triage import TriageFilter
from nautobot_event_tracker.models import EventType, LLMModel, LLMProvider, LLMUsageRecord
from nautobot_event_tracker.services.exceptions import LLMConfigurationError
from nautobot_event_tracker.tests import fixtures
from nautobot_event_tracker.tests.fixtures import TRIAGE_SETTINGS, FakeComplete


class TriageTestCase(TestCase):
    """A configured triage filter and a way to hand it one event."""

    @classmethod
    def setUpTestData(cls):
        """The event types and the registered model triage is configured to use."""
        fixtures.create_event_types()
        fixtures.create_llmmodel()

    def setUp(self):
        """The pieces `build()` fills in per test."""
        super().setUp()
        self.fake = None
        self.topic_config = None

    def build(self, *, fake=None, triage_settings=None, topic_settings=None, clock=None):
        """A TriageFilter over a loaded configuration, and the loaded topic to go with it."""
        self.fake = fake if fake is not None else FakeComplete()
        with fixtures.ingestion_settings(
            triage={**TRIAGE_SETTINGS, **(triage_settings or {})},
            topics={"network.events": {**fixtures.INGESTION_TOPIC, **(topic_settings or {})}},
        ):
            loaded = config.load()
        self.topic_config = loaded.topics["network.events"]
        if clock is None:
            return TriageFilter(loaded, complete=self.fake)
        return TriageFilter(loaded, complete=self.fake, clock=clock)

    @staticmethod
    def event(**overrides):
        """One normalized event, the shape the pipeline hands over."""
        values = {
            "topic": "network.events",
            "event_type_name": "Test Interface Down",
            "title": "Interface ethernet-1/1 is down",
            "severity": "major",
            "description": "",
            "dedup_key": "Test Interface Down:leaf-01",
            "occurred_at": timezone.now(),
            "payload": {"host": "leaf-01"},
        }
        values.update(overrides)
        return NormalizedEvent(**values)

    def accepted(self):
        """The pre-filter result triage runs on."""
        event_type = EventType.objects.get(name="Test Interface Down")
        return FilterResult(ACCEPT, event_type)

    def decide(self, triage=None, event=None, filter_result=None, **kwargs):
        """One event through the filter."""
        triage = triage if triage is not None else self.build()
        return triage.decide(
            event if event is not None else self.event(),
            self.topic_config,
            filter_result if filter_result is not None else self.accepted(),
            **kwargs,
        )


class TestTheVerdicts(TriageTestCase):
    """Each action the model may choose, read with appropriate suspicion (T3)."""

    def test_accept(self):
        """The default verdict passes the event on unchanged."""
        result = self.decide()
        self.assertEqual(result.decision.action, ACTION_ACCEPT)
        self.assertTrue(result.triaged)
        self.assertFalse(result.errored)

    def test_every_judged_event_leaves_a_usage_record(self):
        """L1, seen from triage's side: the decision is on the books."""
        result = self.decide()
        record = LLMUsageRecord.objects.get()
        self.assertEqual(result.usage_record_ids, (record.pk,))
        self.assertEqual(record.purpose, "triage")

    def test_suppress_carries_the_models_reason(self):
        """The reason becomes the ticket message, so it must survive the parse."""
        triage = self.build(fake=FakeComplete('{"action": "suppress", "reason": "known flapping optic"}'))
        result = self.decide(triage)
        self.assertEqual(result.decision.action, ACTION_SUPPRESS)
        self.assertEqual(result.decision.reason, "known flapping optic")

    def test_drop(self):
        """Pure noise, on the model's say-so."""
        triage = self.build(fake=FakeComplete('{"action": "drop", "reason": "test traffic"}'))
        self.assertEqual(self.decide(triage).decision.action, ACTION_DROP)

    def test_attach_resolves_the_shortlist_index_to_a_ticket(self):
        """The model names an index; only the app knows which ticket that is."""
        ticket = fixtures.create_ticket(title="The open incident")
        triage = self.build(fake=FakeComplete('{"action": "attach", "reason": "same incident", "ticket": 0}'))
        result = self.decide(triage)
        self.assertEqual(result.decision.action, ACTION_ATTACH)
        self.assertEqual(result.target_ticket_id, ticket.pk)

    def test_an_unparsable_answer_is_an_accept(self):
        """T3 - garbage in, accept out."""
        triage = self.build(fake=FakeComplete("I think this event is quite serious."))
        result = self.decide(triage)
        self.assertEqual(result.decision.action, ACTION_ACCEPT)

    def test_an_unknown_action_is_an_accept(self):
        """The vocabulary is closed."""
        triage = self.build(fake=FakeComplete('{"action": "escalate", "reason": "hmm"}'))
        self.assertEqual(self.decide(triage).decision.action, ACTION_ACCEPT)

    def test_an_attach_outside_the_shortlist_is_an_accept(self):
        """The model cannot point at a ticket it was not shown."""
        fixtures.create_ticket()
        triage = self.build(fake=FakeComplete('{"action": "attach", "reason": "sure", "ticket": 99}'))
        result = self.decide(triage)
        self.assertEqual(result.decision.action, ACTION_ACCEPT)
        self.assertIsNone(result.target_ticket_id)

    def test_an_attach_with_no_index_is_an_accept(self):
        """Attach without a target is not a decision."""
        fixtures.create_ticket()
        triage = self.build(fake=FakeComplete('{"action": "attach", "reason": "sure"}'))
        self.assertEqual(self.decide(triage).decision.action, ACTION_ACCEPT)

    def test_a_failed_call_is_an_accept_and_counted_as_an_error(self):
        """T4 - fail open. The failure itself is on the usage record."""
        triage = self.build(fake=FakeComplete(error=RuntimeError("provider down")))
        result = self.decide(triage)
        self.assertEqual(result.decision.action, ACTION_ACCEPT)
        self.assertTrue(result.errored)
        record = LLMUsageRecord.objects.get()
        self.assertFalse(record.success)
        self.assertEqual(result.usage_record_ids, (record.pk,))


class TestWhatNeverReachesTheModel(TriageTestCase):
    """The short-circuits: free answers stay free."""

    def test_a_recurrence_skips_the_model(self):
        """T2 - an open ticket with the dedup key means S5 already has the answer."""
        fixtures.create_ticket(dedup_key="Test Interface Down:leaf-01")
        triage = self.build()
        result = self.decide(triage)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(result.triaged)
        self.assertEqual(result.decision.action, ACTION_ACCEPT)

    def test_a_resolved_ticket_with_the_key_does_not_short_circuit(self):
        """A terminal ticket is not open work; the event deserves a fresh look."""
        fixtures.create_ticket_in_status(TicketStatusChoices.RESOLVED, dedup_key="Test Interface Down:leaf-01")
        triage = self.build()
        self.decide(triage)
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_topic_opted_out_passes_through(self):
        """Per-topic `triage: False` is the payload-privacy escape hatch."""
        triage = self.build(topic_settings={"triage": False})
        result = self.decide(triage)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(result.triaged)

    def test_a_rule_suppression_is_not_overruled(self):
        """The operator wrote that rule; a model does not get a vote."""
        event_type = EventType.objects.get(name="Test Interface Down")
        rule_suppressed = FilterResult(Decision(ACTION_SUPPRESS, "known-flapper"), event_type)
        triage = self.build()
        result = self.decide(triage, filter_result=rule_suppressed)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(result.decision.reason, "known-flapper")

    def test_a_database_retry_reuses_the_paid_decision(self):
        """T5 - same message identity, one model call."""
        triage = self.build()
        identity = fixtures.broker_message({}, partition=0, offset=7).identity
        first = self.decide(triage, identity=identity)
        second = self.decide(triage, identity=identity)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(first.decision, second.decision)

    def test_a_reused_decision_says_it_came_from_the_memo(self):
        """The pipeline counts model calls, and a retry is not one of them."""
        triage = self.build()
        identity = fixtures.broker_message({}, partition=0, offset=7).identity
        self.assertFalse(self.decide(triage, identity=identity).from_memo)
        self.assertTrue(self.decide(triage, identity=identity).from_memo)

    def test_a_new_message_is_a_new_decision(self):
        """The memo holds one entry: the message in flight."""
        triage = self.build()
        self.decide(triage, identity=fixtures.broker_message({}, partition=0, offset=7).identity)
        self.decide(triage, identity=fixtures.broker_message({}, partition=0, offset=8).identity)
        self.assertEqual(len(self.fake.calls), 2)

    def test_the_same_offset_on_another_partition_is_another_message(self):
        """Kafka numbers offsets per partition, so an offset alone names no particular message."""
        triage = self.build()
        self.decide(triage, identity=fixtures.broker_message({}, partition=0, offset=7).identity)
        self.decide(triage, identity=fixtures.broker_message({}, partition=1, offset=7).identity)
        self.assertEqual(len(self.fake.calls), 2)

    def test_a_broker_without_offsets_still_memoizes(self):
        """Redis pub/sub numbers nothing, and a retry there must not pay twice either."""
        triage = self.build()
        identity = fixtures.broker_message({"host": "leaf-01"}).identity
        self.decide(triage, identity=identity)
        self.decide(triage, identity=identity)
        self.assertEqual(len(self.fake.calls), 1)


class TestThePrompt(TriageTestCase):
    """T7 - bounded, secret-free, and carrying what the call needs."""

    def call_kwargs(self, **build_kwargs):
        """Decide once and return what reached the seam."""
        triage = self.build(**build_kwargs)
        self.decide(triage)
        return self.fake.calls[0]

    def test_the_call_carries_the_configured_limits(self):
        """Timeout, token cap and JSON mode all come from configuration."""
        kwargs = self.call_kwargs(triage_settings={"timeout_seconds": 7, "max_output_tokens": 99})
        self.assertEqual(kwargs["timeout"], 7.0)
        self.assertEqual(kwargs["max_tokens"], 99)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["purpose"], "triage")

    def test_the_prompt_carries_the_event_and_the_payload(self):
        """The model can only judge what it is shown."""
        kwargs = self.call_kwargs()
        user_content = kwargs["messages"][1]["content"]
        self.assertIn("Interface ethernet-1/1 is down", user_content)
        self.assertIn("leaf-01", user_content)

    def test_the_payload_is_capped(self):
        """A payload the size of a config backup must not become a prompt that size."""
        triage = self.build(triage_settings={"max_context_chars": 50})
        self.decide(triage, event=self.event(payload={"blob": "x" * 5000}))
        user_content = self.fake.calls[0]["messages"][1]["content"]
        self.assertIn("truncated", user_content)
        self.assertLess(len(user_content), 1000)

    def test_the_shortlist_prefers_the_events_own_type(self):
        """Spec 6.4: same event type first, newest first."""
        same_type = fixtures.create_ticket(title="Same type ticket")
        other_type = fixtures.create_ticket(
            title="Other type ticket",
            event_type=EventType.objects.get(name="Test Device Unreachable"),
        )
        kwargs = self.call_kwargs()
        user_content = kwargs["messages"][1]["content"]
        self.assertIn(same_type.title, user_content)
        self.assertNotIn(other_type.title, user_content)

    def test_the_shortlist_falls_back_to_other_open_tickets(self):
        """A first-of-its-type event may still belong to a broader incident."""
        other_type = fixtures.create_ticket(
            title="Other type ticket",
            event_type=EventType.objects.get(name="Test Device Unreachable"),
        )
        kwargs = self.call_kwargs()
        self.assertIn(other_type.title, kwargs["messages"][1]["content"])

    def test_an_empty_shortlist_says_attach_is_unavailable(self):
        """The model must not be invited to attach to nothing."""
        kwargs = self.call_kwargs()
        self.assertIn("attach is not available", kwargs["messages"][1]["content"])

    def test_the_shortlist_uses_the_type_the_ticket_will_get(self):
        """Under `unknown_event_type: "default"` the payload's name is not the ticket's type.

        Matching on the raw name would leave exactly these events shown other types' tickets.
        """
        same_type = fixtures.create_ticket(title="Same type ticket")
        triage = self.build()
        self.decide(triage, event=self.event(event_type_name="Something The Catalogue Never Heard Of"))
        self.assertIn(same_type.title, self.fake.calls[0]["messages"][1]["content"])


class TestAttribution(TriageTestCase):
    """T6 - triage acts as `source=ai` with no user, and never through its own writes."""

    def test_the_filter_itself_writes_no_ticket_rows(self):
        """Deciding is not applying; the pipeline owns every write."""
        from nautobot_event_tracker.models import EventTicket, TicketUpdate  # pylint: disable=import-outside-toplevel

        triage = self.build(fake=FakeComplete('{"action": "suppress", "reason": "noise"}'))
        self.decide(triage)
        self.assertFalse(EventTicket.objects.exists())
        self.assertFalse(TicketUpdate.objects.exists())

    def test_the_ai_source_constant_is_what_the_pipeline_applies(self):
        """One spelling of the actor, shared with the service layer."""
        self.assertEqual(TicketSourceChoices.AI, "ai")


class StoppedClock:  # pylint: disable=too-few-public-methods
    """A monotonic clock a test moves by hand. No sleeps, per the spec's house rules."""

    def __init__(self):
        """Start at zero."""
        self.now = 0.0

    def __call__(self):
        """The time the test last set."""
        return self.now


class TestTheModelCache(TriageTestCase):
    """L8 from the consumer's side: the operator's off switch has to reach a running process."""

    def test_the_model_is_re_read_after_the_ttl(self):
        """Unticking Enabled must stop the calls without a restart.

        This is the whole point of the cache having a TTL. Held for the life of the process, the
        `enabled` flag `services.llm` checks is the one that was true at startup, and rule L8's
        promise that a disabled model "refuses every call" is false on the one process that is
        making them.
        """
        clock = StoppedClock()
        triage = self.build(clock=clock)
        self.decide(triage, identity="first")
        self.assertEqual(len(self.fake.calls), 1)

        LLMModel.objects.filter(name="test-model").update(enabled=False)
        clock.now = 61.0

        result = self.decide(triage, identity="second")

        self.assertEqual(len(self.fake.calls), 1, "the disabled model was called anyway")
        self.assertTrue(result.errored)
        self.assertEqual(result.decision.action, ACTION_ACCEPT, "T4 still fails open")

    def test_the_model_is_not_re_read_within_the_ttl(self):
        """The cache is still a cache: a join in front of every event is what it exists to avoid."""
        clock = StoppedClock()
        triage = self.build(clock=clock)
        self.decide(triage, identity="first")

        with self.assertNumQueries(0):
            triage._get_model()  # pylint: disable=protected-access

    def test_a_refusal_is_cached_for_the_ttl_like_an_answer(self):
        """A disabled model is the case this cache exists to notice, not the case it stops helping.

        With the timestamp stamped only on a successful read, the TTL check stays expired for as
        long as the model is disabled: a query, an exception and a log line for every event, on the
        path an operator has just told to stop working.
        """
        clock = StoppedClock()
        triage = self.build(clock=clock)
        self.decide(triage, identity="first")

        LLMModel.objects.filter(name="test-model").update(enabled=False)
        clock.now = 61.0
        self.decide(triage, identity="second")

        with self.assertNumQueries(0):
            with self.assertRaises(LLMConfigurationError):
                triage._get_model()  # pylint: disable=protected-access

    def test_a_refused_call_is_an_error_but_not_a_call(self):
        """`triaged` counts what an operator pays for, and nothing was sent."""
        clock = StoppedClock()
        triage = self.build(clock=clock)
        self.decide(triage, identity="first")

        LLMModel.objects.filter(name="test-model").update(enabled=False)
        clock.now = 61.0
        result = self.decide(triage, identity="second")

        self.assertTrue(result.errored)
        self.assertFalse(result.triaged, "a refusal before any network traffic is not a model call")
        self.assertEqual(result.usage_record_ids, ())

    def test_a_disabled_provider_stops_the_calls_too(self):
        """L8 covers the provider as well, and the cached model carries a cached provider with it."""
        clock = StoppedClock()
        triage = self.build(clock=clock)
        self.decide(triage, identity="first")

        LLMProvider.objects.filter(name="Test Provider").update(enabled=False)
        clock.now = 61.0

        result = self.decide(triage, identity="second")

        self.assertEqual(len(self.fake.calls), 1)
        self.assertTrue(result.errored)
