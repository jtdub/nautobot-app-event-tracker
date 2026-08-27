"""Tests for the retrieval service: what gets indexed, what gets found, and what never happens.

No test reaches a provider: `embed` is a seam, and the fake routes through the real
`services.llm.embed` so every indexing pass leaves a real usage record behind.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from nautobot_event_tracker.choices import (
    LLMPurposeChoices,
    TicketSourceChoices,
    TicketStatusChoices,
)
from nautobot_event_tracker.models import LLMUsageRecord, TicketEmbedding
from nautobot_event_tracker.services import rag as rag_service
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.services.exceptions import LLMCallError, LLMConfigurationError
from nautobot_event_tracker.tests import fixtures


class RagTestCase(TestCase):
    """A closed ticket and an embedding model to index it with."""

    def setUp(self):
        """One closed ticket, one embedding model."""
        self.user = fixtures.create_user()
        self.model = fixtures.create_embedding_model()
        self.ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=self.user, title="leaf-01 interface down"
        )

    def index(self, ticket=None, *, vector=None, **overrides):
        """Index a ticket with retrieval switched on."""
        embed = fixtures.FakeEmbed(vector)
        with fixtures.rag_settings(**overrides):
            row = rag_service.index_ticket(ticket or self.ticket, embed=embed)
        self.embed = embed  # pylint: disable=attribute-defined-outside-init
        return row


class TestTheSettings(TestCase):
    """The `rag` block: defaults, and values that cannot work."""

    def test_retrieval_is_off_by_default(self):
        """An app installed before anybody configured one indexes nothing."""
        with fixtures.app_settings():
            self.assertFalse(rag_service.get_settings().enabled)

    def test_a_provider_and_model_are_required_when_enabled(self):
        """Switching it on without naming a model is worth a sentence."""
        with fixtures.app_settings(rag={"enabled": True}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                rag_service.get_settings()

        self.assertIn("'provider' is required", str(caught.exception))
        self.assertIn("'model' is required", str(caught.exception))

    def test_a_distance_outside_the_cosine_range_is_refused(self):
        """Cosine distance is bounded at 2; a threshold past it quietly returns everything."""
        for value in (0, -1, 2.5, True):
            with self.subTest(value=value):
                with fixtures.app_settings(rag={"max_distance": value}):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        rag_service.get_settings()
                self.assertIn("'max_distance'", str(caught.exception))


class TestTheSchemaAgreesWithTheValidator(fixtures.SchemaAgreementAssertions, TestCase):
    """The `rag` block's half of the contract. The argument is in the shared base."""

    BLOCK = "rag"
    SETTINGS_MODULE = rag_service
    BASE_BLOCK = {"enabled": False}
    PROBES = {
        "max_distance": {"valid": (0.15, 0.5, 2), "invalid": (0, -1, 2.5)},
        "timeout_seconds": {"valid": (30, 0.5), "invalid": (0, -1)},
        "max_document_chars": {"valid": (1, 8000), "invalid": (0, -1)},
        "similar_count": {"valid": (1, 5), "invalid": (0, -1)},
    }


class TestTheDocument(RagTestCase):
    """R3 - what gets embedded, and the two things deliberately left out."""

    def test_it_carries_what_happened_and_what_was_done(self):
        """The resolution is half the value of the corpus."""
        document = rag_service.render_document(self.ticket)

        self.assertIn(self.ticket.title, document)
        self.assertIn(self.ticket.event_type.name, document)
        # Whatever the close recorded as its resolution - the fixture walks new -> closed, so it is
        # the service's own "closed without a recorded resolution" line rather than a typed one.
        self.assertIn(f"Resolution: {self.ticket.resolution}", document)

    def test_the_raw_payload_is_excluded(self):
        """It is machine noise, it is the attacker-written half, and it would dominate the vector.

        Two unrelated tickets from one chatty device would otherwise look alike because their
        payloads do.
        """
        ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED,
            user=self.user,
            title="payload ticket",
            payload={"secret_looking_noise": "abcdef0123456789"},
        )

        self.assertNotIn("abcdef0123456789", rag_service.render_document(ticket))

    def test_ai_authored_comments_are_excluded(self):
        """12.5 - the corpus is what people concluded, not what a model said.

        Otherwise the app learns from itself: an agent's guess is indexed, retrieved as precedent,
        and read as though somebody had checked it.
        """
        ticket = fixtures.create_ticket(user=self.user, title="commented")
        ticket_service.add_comment(
            ticket=ticket, message="A human wrote this down.", source=TicketSourceChoices.HUMAN, user=self.user
        )
        ticket_service.add_comment(ticket=ticket, message="An agent guessed this.", source=TicketSourceChoices.AI)

        document = rag_service.render_document(ticket)

        self.assertIn("A human wrote this down.", document)
        self.assertNotIn("An agent guessed this.", document)

    def test_it_is_capped(self):
        """A ticket worked over three days has a conversation, not a corpus entry."""
        # A long description rather than a long title: the title is a CharField and would be
        # refused by validation long before it reached the cap this is testing.
        ticket = fixtures.create_ticket(user=self.user, title="wordy", description="x" * 5000)

        self.assertEqual(len(rag_service.render_document(ticket, max_chars=200)), 200)


class TestIndexing(RagTestCase):
    """R4, R8 - one row per ticket, and every call accounted."""

    def test_a_closed_ticket_is_indexed(self):
        """The ordinary case."""
        row = self.index()

        self.assertEqual(row.ticket, self.ticket)
        self.assertEqual(row.dimensions, 3)
        self.assertEqual(row.model, self.model)
        self.assertEqual(TicketEmbedding.objects.count(), 1)

    def test_an_open_ticket_is_not_indexed(self):
        """12.3 - only closed. An open ticket has no resolution to learn from."""
        open_ticket = fixtures.create_ticket(user=self.user, title="still open")

        self.assertIsNone(self.index(open_ticket))
        self.assertFalse(TicketEmbedding.objects.exists())

    def test_retrieval_off_indexes_nothing(self):
        """The switch works before any model call."""
        embed = fixtures.FakeEmbed()
        with fixtures.app_settings():
            self.assertIsNone(rag_service.index_ticket(self.ticket, embed=embed))

        self.assertEqual(embed.calls, [])

    def test_re_indexing_an_unchanged_ticket_costs_nothing(self):
        """R4 - the fingerprint is what makes a re-close free."""
        self.index()

        embed = fixtures.FakeEmbed()
        with fixtures.rag_settings():
            rag_service.index_ticket(self.ticket, embed=embed)

        self.assertEqual(embed.calls, [])
        self.assertEqual(TicketEmbedding.objects.count(), 1)

    def test_re_indexing_a_changed_ticket_replaces_the_row(self):
        """One embedding per ticket, replaced rather than accumulated."""
        first = self.index()
        ticket_service.add_comment(
            ticket=self.ticket, message="And then this happened.", source=TicketSourceChoices.HUMAN, user=self.user
        )

        second = self.index(vector=[0.0, 1.0, 0.0])

        self.assertEqual(TicketEmbedding.objects.count(), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertNotEqual(first.document_fingerprint, second.document_fingerprint)

    def test_every_indexing_call_is_accounted(self):
        """R8 - purpose `embedding`, linked to the ticket, in the table triage's calls land in."""
        self.index()

        record = LLMUsageRecord.objects.get(purpose=LLMPurposeChoices.EMBEDDING)
        self.assertEqual(record.ticket, self.ticket)
        self.assertTrue(record.success)

    def test_a_chat_model_is_refused(self):
        """The registry knows which is which because a person said so."""
        fixtures.create_llmmodel(name="a-chat-model")

        with fixtures.rag_settings(model="a-chat-model"):
            with self.assertRaises(LLMConfigurationError) as caught:
                rag_service.index_ticket(self.ticket, embed=fixtures.FakeEmbed())

        self.assertIn("cannot serve", str(caught.exception))


class TestIndexingNeverBlocksAClose(RagTestCase):
    """R5 - a close is a person finishing work, and must not fail because a model is down."""

    def test_a_failing_model_is_swallowed(self):
        """The quiet entry point, which is what the Job Hook calls."""
        embed = fixtures.FakeEmbed(error=LLMCallError("the provider refused"))

        with fixtures.rag_settings():
            self.assertIsNone(rag_service.index_ticket_quietly(self.ticket, embed=embed))

        self.assertFalse(TicketEmbedding.objects.exists())

    def test_a_broken_configuration_is_swallowed(self):
        """A settings fault must not stop a close either."""
        with fixtures.app_settings(rag={"enabled": True, "max_distance": 9}):
            self.assertIsNone(rag_service.index_ticket_quietly(self.ticket))

    def test_the_loud_entry_point_still_raises(self):
        """A backfill wants to hear about failures; only the close path is quiet."""
        embed = fixtures.FakeEmbed(error=LLMCallError("the provider refused"))

        with fixtures.rag_settings():
            with self.assertRaises(LLMCallError):
                rag_service.index_ticket(self.ticket, embed=embed)


class TestRetrieval(RagTestCase):
    """R2, R6, R7 - within one model, only what a user may see, only what is actually close."""

    def setUp(self):
        """A corpus of two closed tickets, and an open one to search with.

        The searching user is a superuser, because `similar_tickets` restricts to what the user may
        view (R6) and an ordinary user with no granted permissions can view nothing. That is the
        rule working; `test_a_user_sees_no_ticket_they_could_not_open` asserts it on purpose.
        """
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.near = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=self.user, title="leaf-01 interface flapping"
        )
        fixtures.create_ticketembedding(ticket=self.near, model=self.model, vector=[1.0, 0.0, 0.0])
        self.far = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=self.user, title="unrelated power event"
        )
        fixtures.create_ticketembedding(ticket=self.far, model=self.model, vector=[0.0, 1.0, 0.0])
        self.open_ticket = fixtures.create_ticket(user=self.user, title="leaf-01 interface down again")

    def search(self, **overrides):
        """Search with a vector pointing at `near`."""
        embed = fixtures.FakeEmbed([1.0, 0.0, 0.0])
        with fixtures.rag_settings(**overrides):
            return rag_service.similar_tickets(self.open_ticket, user=self.user, embed=embed)

    def test_the_nearest_closed_ticket_is_found(self):
        """The whole point: we have seen this before."""
        matches = self.search()

        self.assertEqual([match.ticket for match in matches], [self.near])

    def test_a_distant_ticket_is_not_called_similar(self):
        """R7 - a nearest-neighbour search always has a nearest neighbour.

        Without a threshold the panel shows the least-unlike rows in the database and trains people
        to ignore it. An orthogonal vector is distance 1.0, well past the default.
        """
        self.assertNotIn(self.far, [match.ticket for match in self.search()])

    def test_nothing_is_returned_when_nothing_is_close(self):
        """The honest answer to "have we seen this" is often no."""
        embed = fixtures.FakeEmbed([0.0, 0.0, 1.0])
        with fixtures.rag_settings():
            matches = rag_service.similar_tickets(self.open_ticket, user=self.user, embed=embed)

        self.assertEqual(matches, [])

    def test_vectors_from_another_model_are_never_compared(self):
        """R2 - cosine distance across two models is a number, and it means nothing."""
        other_model = fixtures.create_embedding_model(name="a-different-embedding-model")
        stranger = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=self.user, title="indexed under another model"
        )
        fixtures.create_ticketembedding(ticket=stranger, model=other_model, vector=[1.0, 0.0, 0.0])

        self.assertNotIn(stranger, [match.ticket for match in self.search()])

    def test_a_ticket_is_never_similar_to_itself(self):
        """A closed ticket searching finds everything but itself."""
        embed = fixtures.FakeEmbed([1.0, 0.0, 0.0])
        with fixtures.rag_settings():
            matches = rag_service.similar_tickets(self.near, user=self.user, embed=embed)

        self.assertNotIn(self.near, [match.ticket for match in matches])

    def test_retrieval_off_returns_nothing(self):
        """And makes no model call doing it."""
        embed = fixtures.FakeEmbed()
        with fixtures.app_settings():
            self.assertEqual(rag_service.similar_tickets(self.open_ticket, user=self.user, embed=embed), [])

        self.assertEqual(embed.calls, [])

    def test_a_broken_configuration_returns_nothing_rather_than_raising(self):
        """A panel is not a place to surface an exception."""
        with fixtures.app_settings(rag={"enabled": True, "max_distance": 9}):
            self.assertEqual(rag_service.similar_tickets(self.open_ticket, user=self.user), [])

    def test_a_closed_ticket_reuses_its_stored_vector(self):
        """No model call to look at a page whose vector is already in the corpus."""
        embed = fixtures.FakeEmbed()
        with fixtures.rag_settings():
            rag_service.similar_tickets(self.near, user=self.user, embed=embed)

        self.assertEqual(embed.calls, [])

    def test_a_user_sees_no_ticket_they_could_not_open(self):
        """R6 - a panel surfacing a ticket somebody cannot open is a leak dressed as a feature."""
        stranger = fixtures.create_user(username="no-permissions")
        embed = fixtures.FakeEmbed([1.0, 0.0, 0.0])

        with fixtures.rag_settings():
            matches = rag_service.similar_tickets(self.open_ticket, user=stranger, embed=embed)

        self.assertEqual(matches, [])

    def test_the_distance_is_rendered_as_a_word(self):
        """An operator does not want to read 0.412."""
        self.assertEqual(rag_service.SimilarTicket(ticket=None, distance=0.09).closeness, "very similar")
        self.assertEqual(rag_service.SimilarTicket(ticket=None, distance=0.15).closeness, "similar")
        self.assertEqual(rag_service.SimilarTicket(ticket=None, distance=0.55).closeness, "loosely similar")
