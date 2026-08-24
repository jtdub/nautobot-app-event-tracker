"""Retrieval over closed tickets: the one module that speaks to pgvector.

When a ticket closes it becomes a document - what happened, and what was done about it. New
tickets are matched against those documents and the closest few are shown to a person.

The rule that shapes everything here is R9, and it is a refusal rather than a feature: **nothing
retrieved is ever put into a prompt.** The corpus is built from tickets whose payloads were written
by whoever can put a line on a consumed topic. A single ticket being read by a model was already
true; indexing changes the reach, because a document that steers one investigation becomes a
document that can be retrieved for every future ticket resembling it. A panel keeps a person
between the corpus and any decision. Section 9 of the Phase 5A spec argues it at length, and
`tests/test_guards.py` asserts the import direction that makes it true.

Rules implemented here, referenced by number from the Phase 5A spec:

* **R1** - pgvector's query surface lives in this module alone. (The field type is in `models.py`,
  because a Django model cannot import a field lazily; the *searching* is here.)
* **R2** - a vector is only ever compared with vectors from its own model. Cosine distance between
  two models' vectors is a number and it is meaningless.
* **R3** - what is embedded is a rendered document, never the raw payload.
* **R4** - one embedding per ticket, replaced rather than accumulated.
* **R5** - indexing never blocks a close. Every failure is caught and logged.
* **R6** - retrieval respects the requesting user's permissions.
* **R7** - distance is a threshold, not a ranking. Past it, the answer is "no".
* **R8** - every embedding call is accounted, through `services.llm` (L1).
* **R9** - nothing retrieved reaches a prompt.
"""

import hashlib
import logging
from dataclasses import dataclass

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from nautobot.apps.utils import deepmerge

from nautobot_event_tracker.choices import (
    TERMINAL_STATUSES,
    LLMModelKindChoices,
    LLMPurposeChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import EventTicket, TicketEmbedding
from nautobot_event_tracker.services import llm as llm_service
from nautobot_event_tracker.services.exceptions import LLMError

logger = logging.getLogger(__name__)

#: Defaults for the `rag` block, applied per key here rather than left to Nautobot's top-level
#: `PLUGINS_CONFIG` merge, for the reason `ingestion.config` documents.
DEFAULTS = {
    "enabled": False,
    "provider": "",
    "model": "",
    "max_document_chars": 8000,
    "similar_count": 5,
    # Cosine distance, so 0 is identical and 2 is opposite. Past this the panel says it has not
    # seen this before rather than showing the least-unlike rows in the database (R7).
    #
    # 0.15, measured rather than guessed. Against nomic-embed-text with real tickets: a genuinely
    # related problem scores 0.09-0.10, and complete nonsense - "the coffee machine in the London
    # office is broken", against a corpus of network faults - scores 0.22-0.24. The first draft of
    # this used 0.6, which would have shown the coffee machine and called it very similar.
    #
    # Short network-ticket texts sit closer together than intuition suggests: they share vocabulary
    # (device names, "down", "alarm") even when the problems are unrelated, so the useful band is
    # much narrower than a general-purpose default. Tune it against your own corpus and model -
    # docs/admin/rag.md says how - and err low, because a quiet panel is ignored far less than one
    # that cries wolf.
    "max_distance": 0.15,
    "timeout_seconds": 30,
}

#: How many of a ticket's comments go into its document. A ticket worked over three days has a
#: conversation; the document wants the shape of it, not every line.
MAX_COMMENTS = 20


@dataclass(frozen=True)
class RagSettings:  # pylint: disable=too-many-instance-attributes
    """The `rag` block, parsed and checked."""

    enabled: bool
    provider: str
    model: str
    max_document_chars: int
    similar_count: int
    max_distance: float
    timeout_seconds: float


@dataclass(frozen=True)
class SimilarTicket:
    """One neighbour, as the panel needs it."""

    ticket: object
    distance: float

    @property
    def closeness(self):
        """The distance as a word, because an operator does not want to read 0.412.

        Deliberately coarse. The number is a cosine distance whose absolute value means little to
        anybody; what a person needs is whether this is worth opening.

        The bands are calibrated with the default threshold, from the same measurements: a related
        problem lands around 0.09, so "very similar" has to mean something tighter than intuition
        suggests. Raise `max_distance` and the third band starts appearing.
        """
        if self.distance <= 0.10:
            return "very similar"
        if self.distance <= 0.20:
            return "similar"
        return "loosely similar"


def get_settings():
    """The `rag` block with defaults applied per key, refusing a value that cannot work.

    Raises `ImproperlyConfigured`, deliberately outside any family a caller handles: a settings
    fault does not repair itself between two closes, and it is not "indexing failed".
    """
    configured = django_settings.PLUGINS_CONFIG.get("nautobot_event_tracker", {}).get("rag") or {}
    merged = deepmerge(DEFAULTS, configured)

    problems = []
    if not isinstance(merged["enabled"], bool):
        problems.append(f"'enabled' must be a boolean, got {merged['enabled']!r}")
    for key in ("max_document_chars", "similar_count"):
        value = merged[key]
        # The `bool` clause is the half that is easy to leave out: in Python `True` is an `int`.
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"'{key}' must be a positive integer, got {value!r}")
    distance = merged["max_distance"]
    if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not 0 < distance <= 2:
        # Cosine distance is bounded at 2. A threshold outside that is not a stricter filter, it is
        # a misunderstanding, and it would quietly return everything.
        problems.append(f"'max_distance' must be a number between 0 and 2, got {distance!r}")
    timeout = merged["timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        problems.append(f"'timeout_seconds' must be a positive number, got {timeout!r}")
    if merged["enabled"] is True:
        for key in ("provider", "model"):
            if not merged[key]:
                problems.append(f"'{key}' is required when rag is enabled")

    if problems:
        raise ImproperlyConfigured("nautobot_event_tracker: rag " + "; ".join(problems))

    return RagSettings(
        enabled=merged["enabled"],
        provider=str(merged["provider"]),
        model=str(merged["model"]),
        max_document_chars=merged["max_document_chars"],
        similar_count=merged["similar_count"],
        max_distance=float(merged["max_distance"]),
        timeout_seconds=float(merged["timeout_seconds"]),
    )


def render_document(ticket, *, max_chars=None):
    """R3 - what gets embedded: what happened, and what was done about it.

    The raw payload is deliberately absent. It is machine noise, it is the half written by whoever
    emits the events, and by volume it would dominate the vector - two unrelated tickets from one
    chatty device would look alike because their payloads do.

    AI-authored comments are absent too (12.5). The corpus should be what people concluded, not
    what a model said, or the app starts learning from itself: an agent's guess gets indexed,
    retrieved as precedent, and read as though somebody had checked it.
    """
    lines = [
        f"Title: {ticket.title}",
        f"Event type: {ticket.event_type.name}",
        f"Severity: {ticket.severity}",
    ]
    if ticket.description:
        lines.append(f"Description: {ticket.description}")
    if ticket.resolution:
        lines.append(f"Resolution: {ticket.resolution}")

    comments = (
        ticket.updates.filter(update_type=UpdateTypeChoices.COMMENT, source=TicketSourceChoices.HUMAN)
        .order_by("created")
        .values_list("message", flat=True)[:MAX_COMMENTS]
    )
    if comments:
        lines.append("Notes:")
        lines.extend(f"- {comment}" for comment in comments)

    document = "\n".join(lines)
    cap = max_chars if max_chars is not None else get_settings().max_document_chars
    return document[:cap]


def document_fingerprint(document):
    """A digest of the document, so re-closing a ticket that did not change costs nothing."""
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def index_ticket(ticket, *, embed=None):
    """R4 - index one closed ticket, replacing whatever it had before.

    Returns the `TicketEmbedding`, or None when there was nothing to do: rag is off, the ticket is
    not closed, or its document is unchanged since last time.

    `embed` is the test seam, defaulting to `services.llm.embed`. No test reaches a provider.

    Raises rather than swallowing. The caller that must not fail - the Job Hook, which runs when a
    person closes a ticket - is where R5 lives, because "never block a close" is a statement about
    that path rather than about this function. A management command backfilling the corpus wants
    to hear about failures.
    """
    settings = get_settings()
    if not settings.enabled:
        return None
    if ticket.status != TicketStatusChoices.CLOSED:
        # Only closed tickets (12.3). A resolved ticket may still be reopened, and its resolution
        # is not yet the last word on the problem.
        return None

    document = render_document(ticket, max_chars=settings.max_document_chars)
    fingerprint = document_fingerprint(document)

    existing = TicketEmbedding.objects.filter(ticket=ticket).first()
    model = llm_service.get_model(settings.provider, settings.model, kind=LLMModelKindChoices.EMBEDDING)
    if existing is not None and existing.document_fingerprint == fingerprint and existing.model_id == model.pk:
        # Same words, same model, so the same vector. Re-closing a ticket nobody edited is free.
        return existing

    call = embed if embed is not None else llm_service.embed
    result = call(
        model=model,
        text=document,
        purpose=LLMPurposeChoices.EMBEDDING,
        ticket=ticket,
        timeout=settings.timeout_seconds,
    )

    values = {
        "embedding": result.vector,
        "document": document,
        "model": model,
        "dimensions": result.dimensions,
        "document_fingerprint": fingerprint,
        "indexed_at": timezone.now(),
    }
    embedding, _ = TicketEmbedding.objects.update_or_create(ticket=ticket, defaults=values)
    logger.info("Indexed closed ticket %s (%d dimensions)", ticket.pk, result.dimensions)
    return embedding


def index_ticket_quietly(ticket, *, embed=None):
    """R5 - index, and never let the attempt reach the caller.

    The Job Hook's entry point. A close is a person finishing work, and it must not fail because a
    model endpoint is down - the same posture as the enrichment resolver's (E5) and triage's (T4),
    for a third path. Returns the embedding, or None when anything at all went wrong.
    """
    try:
        return index_ticket(ticket, embed=embed)
    except (LLMError, ImproperlyConfigured) as error:
        logger.warning("Could not index closed ticket %s: %s", ticket.pk, error)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not index closed ticket %s", ticket.pk)
    return None


def visible_embeddings(queryset, user):
    """Narrow a TicketEmbedding queryset to embeddings of tickets `user` may view - rule R6.

    A `document` is a verbatim copy of its ticket, so every route to a corpus row is a route to
    ticket text. Gated on `view_ticketembedding` alone, holding that permission reads every closed
    ticket in the deployment however tightly `EventTicket` is constrained, because nothing carries
    the constraint across the relation.

    Used by the REST and UI viewsets, which read the corpus as a list of rows. `similar_tickets`
    below does not go through it and should not: it restricts `EventTicket` directly, because it
    needs the closed ones as the set to search within, where these two need whichever corpus rows
    happen to hang off readable tickets. Same rule, two shapes. It lives here rather than beside
    either viewset so that both, and anything that reads the corpus later, answer "which tickets
    may this person see" the way the query in this module already answers it.
    """
    if not user.is_authenticated:
        return queryset.none()
    return queryset.filter(ticket__in=EventTicket.objects.restrict(user, "view").values("pk"))


def similar_tickets(ticket, *, user, limit=None, embed=None):
    """R6, R7 - the closed tickets nearest this one, that this user may see.

    Returns a list of `SimilarTicket`, nearest first, never including the ticket itself.

    Restricted through Nautobot's own queryset restriction, so the panel can never surface a ticket
    somebody could not open - an information leak dressed as a feature. And bounded by
    `max_distance`: a nearest-neighbour search always has a nearest neighbour, so without a
    threshold the panel would confidently show the five least-unlike rows in the database and train
    people to ignore it.

    Returns an empty list rather than raising, for every reason it might: rag off, no model, the
    ticket not indexable, nothing in the corpus. A panel is not a place to surface an exception.
    """
    try:
        settings = get_settings()
    except ImproperlyConfigured as error:
        logger.warning("Cannot search for similar tickets: %s", error)
        return []
    if not settings.enabled:
        return []

    vector = _query_vector(ticket, settings, embed=embed)
    if vector is None:
        return []

    from pgvector.django import CosineDistance  # pylint: disable=import-outside-toplevel

    # R2 - within one model only, and one dimension follows from that. Comparing across models
    # would return a number that means nothing.
    visible = EventTicket.objects.restrict(user, "view").filter(status=TicketStatusChoices.CLOSED)
    rows = (
        TicketEmbedding.objects.filter(
            model=vector.model,
            dimensions=len(vector.values),
            ticket__in=visible,
        )
        .exclude(ticket=ticket)
        .annotate(distance=CosineDistance("embedding", vector.values))
        .filter(distance__lte=settings.max_distance)
        .select_related("ticket__event_type")
        .order_by("distance")[: limit or settings.similar_count]
    )
    return [SimilarTicket(ticket=row.ticket, distance=float(row.distance)) for row in rows]


@dataclass(frozen=True)
class _QueryVector:
    """The vector to search with, and the model it belongs to."""

    values: list
    model: object


def _query_vector(ticket, settings, *, embed=None):
    """The vector for the ticket being viewed, or None when there cannot be one.

    A closed ticket that is already indexed reuses its stored vector - no model call to look at a
    page. Anything else is embedded on the spot, which is the ordinary case: the panel is for open
    tickets, and an open ticket is never in the corpus.
    """
    try:
        model = llm_service.get_model(settings.provider, settings.model, kind=LLMModelKindChoices.EMBEDDING)
    except (LLMError, ImproperlyConfigured) as error:
        logger.warning("Cannot search for similar tickets: %s", error)
        return None

    if ticket.status in TERMINAL_STATUSES:
        stored = TicketEmbedding.objects.filter(ticket=ticket, model=model).first()
        if stored is not None:
            return _QueryVector(values=list(stored.embedding), model=model)

    call = embed if embed is not None else llm_service.embed
    try:
        result = call(
            model=model,
            text=render_document(ticket, max_chars=settings.max_document_chars),
            purpose=LLMPurposeChoices.EMBEDDING,
            ticket=ticket,
            timeout=settings.timeout_seconds,
        )
    except (LLMError, ImproperlyConfigured) as error:
        # `ImproperlyConfigured` as well as the LLM family, because a missing `llm` extra raises
        # outside that family deliberately - and this became reachable when `pgvector` was made a
        # required dependency while litellm stayed optional. Without it, an install without the
        # extra plus `rag.enabled` returns HTTP 500 on every open ticket's page. A panel is not a
        # place to surface an exception.
        logger.warning("Could not embed ticket %s to search with: %s", ticket.pk, error)
        return None
    return _QueryVector(values=result.vector, model=model)
