"""Models for Event Tracker.

Every mutation of EventTicket and TicketUpdate goes through
`nautobot_event_tracker.services.tickets`. Nothing else writes to them. See ADR 0001.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from nautobot.apps.constants import CHARFIELD_MAX_LENGTH
from nautobot.apps.models import BaseModel, ChangeLoggedModel, OrganizationalModel, PrimaryModel, extras_features
from pgvector.django import VectorField

from nautobot_event_tracker.choices import (
    AGENT_RUN_LIVE_STATUSES,
    ATTACHMENT_UPDATE_TYPES,
    PROVIDER_TYPES_REQUIRING_A_URL,
    TERMINAL_STATUSES,
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMModelKindChoices,
    LLMProviderTypeChoices,
    LLMPurposeChoices,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)


class TicketUpdateImmutableError(Exception):
    """Raised when code attempts to modify or delete a persisted TicketUpdate."""


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class EventType(OrganizationalModel):  # pylint: disable=too-many-ancestors
    """A kind of network event the system knows about."""

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    default_severity = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=SeverityChoices,
        default=SeverityChoices.MINOR,
        help_text="Severity applied to a ticket of this type when the caller does not specify one.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="A disabled type cannot be used for new tickets. Existing tickets keep theirs.",
    )

    class Meta:
        """Meta class."""

        ordering = ["name"]
        verbose_name = "Event Type"
        verbose_name_plural = "Event Types"

    def __str__(self):
        """Stringify instance."""
        return self.name


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class EventTicket(PrimaryModel):  # pylint: disable=too-many-ancestors
    """A ticket raised for one or more network events."""

    title = models.CharField(max_length=CHARFIELD_MAX_LENGTH)
    event_type = models.ForeignKey(
        to=EventType,
        on_delete=models.PROTECT,
        related_name="tickets",
    )
    status = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=TicketStatusChoices,
        default=TicketStatusChoices.NEW,
        db_index=True,
        help_text="Service-owned. Change it through services.tickets.transition(), never directly.",
    )
    severity = models.CharField(max_length=CHARFIELD_MAX_LENGTH, choices=SeverityChoices, db_index=True)
    source = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=TicketSourceChoices,
        default=TicketSourceChoices.HUMAN,
        help_text=(
            "How the ticket came to exist. The service layer always sets this explicitly; the "
            "default only applies to a ticket built from a form."
        ),
    )
    description = models.TextField(blank=True)
    assigned_to = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="event_tickets",
        null=True,
        blank=True,
    )
    dedup_key = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text="Idempotency key. A repeat event with this key joins the open ticket instead of opening a new one.",
    )
    event_count = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True, help_text="Raw event data. Populated from Phase 2.")

    # A ticket has no business-level natural key: two tickets may legitimately share a title, and
    # the dedup key is optional and reused across a ticket's lifetime. Fall back to the primary
    # key, as Nautobot core does for ObjectChange and friends.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["-last_seen"]
        verbose_name = "Event Ticket"
        verbose_name_plural = "Event Tickets"
        permissions = [("transition_eventticket", "Can transition event ticket status")]
        indexes = [
            models.Index(fields=["dedup_key", "status"], name="event_ticket_dedup_idx"),
            # The two range scans the analytics dashboard makes (Phase 5B spec, section 8.2).
            # `created` is inherited from Nautobot's change-logged base and `closed_at` is declared
            # above; neither carried an index, and the ticket-flow panel groups by both. This is
            # the whole of the schema that phase adds - it introduces no model and no counter.
            models.Index(fields=["created"], name="event_ticket_created_idx"),
            models.Index(fields=["closed_at"], name="event_ticket_closed_idx"),
            # The dashboard's severity split asks "which tickets are open right now", so it is the
            # one query on that page with no time bound and nothing to bound it by. Without this
            # it scans the whole table on every render, and `event_ticket_dedup_idx` cannot serve
            # it - a leading `dedup_key` does not help a `status NOT IN`.
            models.Index(fields=["status", "severity"], name="event_ticket_open_sev_idx"),
        ]

    def __str__(self):
        """Stringify instance."""
        return self.title

    @property
    def is_open(self):
        """True when the ticket is neither resolved nor closed."""
        return self.status not in TERMINAL_STATUSES

    def clean(self):
        """Validate shape, never actor. Actor rules live in the service layer (ADR 0001)."""
        super().clean()
        errors = {}

        is_terminal = self.status in TERMINAL_STATUSES

        # C1 - timestamps agree with status.
        expects_resolved_at = is_terminal
        if expects_resolved_at and self.resolved_at is None:
            errors["resolved_at"] = f"A ticket with status '{self.status}' must have a resolved time."
        if not expects_resolved_at and self.resolved_at is not None:
            errors["resolved_at"] = f"A ticket with status '{self.status}' must not have a resolved time."

        expects_closed_at = self.status == TicketStatusChoices.CLOSED
        if expects_closed_at and self.closed_at is None:
            errors["closed_at"] = f"A ticket with status '{self.status}' must have a closed time."
        if not expects_closed_at and self.closed_at is not None:
            errors["closed_at"] = f"A ticket with status '{self.status}' must not have a closed time."

        if self.resolved_at and self.closed_at and self.closed_at < self.resolved_at:
            errors["closed_at"] = "Closed time cannot be earlier than resolved time."

        # C2 - resolution text agrees with status.
        expects_resolution = is_terminal
        if expects_resolution and not self.resolution:
            errors["resolution"] = f"A ticket with status '{self.status}' must have a resolution."
        if not expects_resolution and self.resolution:
            errors["resolution"] = f"A ticket with status '{self.status}' must not have a resolution."

        if errors:
            raise ValidationError(errors)


class TicketUpdate(BaseModel, ChangeLoggedModel):
    """One append-only entry in a ticket's history.

    Deliberately not a PrimaryModel: an audit row should not carry tags, custom fields, or a change
    log of its own.
    """

    ticket = models.ForeignKey(
        to=EventTicket,
        on_delete=models.CASCADE,
        related_name="updates",
    )
    update_type = models.CharField(max_length=CHARFIELD_MAX_LENGTH, choices=UpdateTypeChoices)
    source = models.CharField(max_length=CHARFIELD_MAX_LENGTH, choices=TicketSourceChoices)
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="event_ticket_updates",
        null=True,
        blank=True,
    )
    message = models.TextField(blank=True)
    from_status = models.CharField(max_length=CHARFIELD_MAX_LENGTH, choices=TicketStatusChoices, blank=True)
    to_status = models.CharField(max_length=CHARFIELD_MAX_LENGTH, choices=TicketStatusChoices, blank=True)
    related_object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.PROTECT,
        related_name="event_ticket_updates",
        null=True,
        blank=True,
    )
    related_object_id = models.UUIDField(null=True, blank=True)
    related_object = GenericForeignKey(ct_field="related_object_type", fk_field="related_object_id")

    # An audit row is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["created"]
        get_latest_by = "created"
        verbose_name = "Ticket Update"
        verbose_name_plural = "Ticket Updates"

    def __str__(self):
        """Stringify instance."""
        return f"{self.get_update_type_display()} on {self.ticket_id}"

    def clean(self):
        """C3 - an update's payload and actor must match its type.

        Each rule reports every field it objects to, so a caller fixes one form rather than
        discovering the next problem on the next attempt.
        """
        super().clean()
        errors = {}
        for check in (self._check_message, self._check_status_fields, self._check_related_object, self._check_actor):
            errors.update(check())

        if errors:
            raise ValidationError(errors)

    def _check_message(self):
        """A comment is nothing without its text."""
        if self.update_type == UpdateTypeChoices.COMMENT and not self.message:
            return {"message": "A comment must have a message."}
        return {}

    def _check_status_fields(self):
        """Only a status change carries statuses, and it must carry two different ones."""
        if self.update_type != UpdateTypeChoices.STATUS_CHANGE:
            return {
                field: f"An update of type '{self.update_type}' must not record a {label} status."
                for field, label in (("from_status", "from"), ("to_status", "to"))
                if getattr(self, field)
            }

        errors = {}
        if not self.from_status:
            errors["from_status"] = "A status change must record the status it moved from."
        if not self.to_status:
            errors["to_status"] = "A status change must record the status it moved to."
        if self.from_status and self.to_status and self.from_status == self.to_status:
            errors["to_status"] = "A status change must move between two different statuses."
        return errors

    def _check_related_object(self):
        """Only an attach or detach carries a related object, and it must carry both halves."""
        halves = {"related_object_type": "type", "related_object_id": "ID"}
        if self.update_type in ATTACHMENT_UPDATE_TYPES:
            return {
                field: f"An attachment update must record the related object {label}."
                for field, label in halves.items()
                if getattr(self, field) is None
            }
        return {
            field: f"An update of type '{self.update_type}' must not record a related object {label}."
            for field, label in halves.items()
            if getattr(self, field) is not None
        }

    def _check_actor(self):
        """S4 at the row level: a human update names its user, a machine update does not."""
        if self.source == TicketSourceChoices.HUMAN and self.user is None:
            return {"user": "An update from a human must record the acting user."}
        if self.source in (TicketSourceChoices.AI, TicketSourceChoices.SYSTEM) and self.user is not None:
            return {"user": f"An update from '{self.source}' must not record an acting user."}
        return {}

    def save(self, *args, **kwargs):
        """Append-only: refuse to modify a row that already exists."""
        if self.present_in_database:
            raise TicketUpdateImmutableError(
                "TicketUpdate is append-only; an existing update cannot be modified. Record a new update instead."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Append-only: refuse to delete.

        Note that Django's cascade collector and `QuerySet.delete()` bypass this method, so
        deleting a ticket still removes its updates. The real protection is that no API or UI
        route reaches either operation.
        """
        raise TicketUpdateImmutableError("TicketUpdate is append-only; an update cannot be deleted.")


class IngestionStats(BaseModel):
    """Counters for one consumer, one topic, one time bucket.

    Deliberately neither a PrimaryModel nor change-logged. The consumer rewrites a bucket row every
    few seconds, so change logging it would write an ObjectChange per flush and bury the change log
    under a record of arithmetic. It is derived data: what the consumer saw, not what anyone meant.

    Written only by `ingestion.stats.StatsRecorder`. No UI or API route offers a write method.
    """

    consumer_name = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        db_index=True,
        help_text="The consumer process these counts came from.",
    )
    topic = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        db_index=True,
        help_text="Broker topic or channel.",
    )
    bucket_start = models.DateTimeField(
        db_index=True,
        help_text="Start of the window these counts cover.",
    )
    received = models.PositiveIntegerField(default=0, help_text="Messages taken from the broker.")
    errored = models.PositiveIntegerField(default=0, help_text="Messages that could not be decoded or mapped.")
    dropped = models.PositiveIntegerField(default=0, help_text="Messages discarded by the pre-filter.")
    tickets_opened = models.PositiveIntegerField(default=0, help_text="Messages that opened a new ticket.")
    tickets_joined = models.PositiveIntegerField(default=0, help_text="Messages that joined an existing ticket.")
    suppressed = models.PositiveIntegerField(
        default=0,
        help_text="Messages a suppression rule accepted. Counted under opened or joined as well.",
    )
    triaged = models.PositiveIntegerField(
        default=0,
        help_text="Messages LLM triage actually judged. A memo counter, outside the accounting invariant.",
    )
    triage_attached = models.PositiveIntegerField(
        default=0,
        help_text="Messages triage attached to an existing ticket. Counted under joined as well.",
    )
    triage_errors = models.PositiveIntegerField(
        default=0,
        help_text="Triage calls that failed and fell back to accept (rule T4).",
    )
    enriched = models.PositiveIntegerField(
        default=0,
        help_text="Messages that attached at least one object the enrichment resolver found.",
    )
    enrichment_misses = models.PositiveIntegerField(
        default=0,
        help_text="Enrichment rules that found nothing they should have found (rules E5 to E7).",
    )
    drops_by_reason = models.JSONField(
        default=dict,
        blank=True,
        help_text="Drop counts keyed by the rule or filter that refused the message.",
    )
    last_message_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Broker timestamp of the newest message counted here.",
    )

    # A counter row is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["-bucket_start", "consumer_name", "topic"]
        get_latest_by = "bucket_start"
        verbose_name = "Ingestion Stats"
        verbose_name_plural = "Ingestion Stats"
        constraints = [
            models.UniqueConstraint(
                fields=["consumer_name", "topic", "bucket_start"],
                name="event_tracker_stats_bucket_unique",
            ),
        ]

    def __str__(self):
        """Stringify instance."""
        return f"{self.consumer_name} {self.topic} {self.bucket_start:%Y-%m-%d %H:%M}"

    @property
    def accounted_for(self):
        """The counting invariant's right-hand side.

        `received` must equal this. `suppressed` is not a term: a suppressed message still opened
        or joined a ticket, so it is already counted there.
        """
        return self.errored + self.dropped + self.tickets_opened + self.tickets_joined


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class LLMProvider(PrimaryModel):  # pylint: disable=too-many-ancestors
    """An LLM endpoint the app may call, registered by an operator (ADR 0006).

    Carries no credentials of its own: the ExternalIntegration it points at holds the endpoint URL,
    and that integration's SecretsGroup holds the API key. Nothing key-shaped lives on this model,
    in PLUGINS_CONFIG, or in a log line (rule L3 from the Phase 3 spec).
    """

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    provider_type = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=LLMProviderTypeChoices,
        default=LLMProviderTypeChoices.OPENAI_COMPATIBLE,
        help_text="Which API protocol the endpoint speaks. Selects how the service layer addresses it.",
    )
    external_integration = models.ForeignKey(
        to="extras.ExternalIntegration",
        on_delete=models.PROTECT,
        related_name="llm_providers",
        help_text="Carries the endpoint URL and, through its secrets group, the API key.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="A disabled provider refuses every call before any network traffic (rule L8).",
    )

    class Meta:
        """Meta class."""

        ordering = ["name"]
        verbose_name = "LLM Provider"
        verbose_name_plural = "LLM Providers"

    def __str__(self):
        """Stringify instance."""
        return self.name

    def clean(self):
        """A self-hosted endpoint is unreachable without a URL to reach it at."""
        super().clean()
        if (
            self.provider_type in PROVIDER_TYPES_REQUIRING_A_URL
            and self.external_integration_id is not None
            and not self.external_integration.remote_url
        ):
            raise ValidationError(
                {
                    "external_integration": (
                        f"A '{self.get_provider_type_display()}' provider needs an external "
                        "integration with a remote URL."
                    )
                }
            )


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class LLMModel(PrimaryModel):  # pylint: disable=too-many-ancestors
    """One model available from a provider, with its parameters and cost metadata (ADR 0006)."""

    provider = models.ForeignKey(
        to=LLMProvider,
        on_delete=models.PROTECT,
        related_name="models",
    )
    name = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text="The model identifier sent on the wire, e.g. 'gpt-4o-mini' or 'llama-3.1-70b'.",
    )
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    enabled = models.BooleanField(
        default=True,
        help_text="A disabled model refuses every call before any network traffic (rule L8).",
    )
    kind = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=LLMModelKindChoices,
        default=LLMModelKindChoices.CHAT,
        db_index=True,
        help_text=(
            "What this model is for. An embedding model and a chat model are not interchangeable, "
            "and the service layer refuses a mismatch in both directions before any network "
            "traffic. Defaults to chat, which is what every model registered before Phase 5A is."
        ),
    )
    input_cost_per_million = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text="USD per one million prompt tokens. Used to compute each call's recorded cost.",
    )
    output_cost_per_million = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text="USD per one million completion tokens.",
    )
    max_output_tokens = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Default completion cap for calls that do not set their own.",
    )
    default_parameters = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Extra request parameters, passed through on every call. Generation parameters only: "
            "extra_body, frequency_penalty, logit_bias, n, presence_penalty, reasoning_effort, "
            "seed, stop, temperature, timeout, top_k, top_p."
        ),
    )

    #: The only keys this field may carry: the parameters that shape an answer, and nothing that
    #: decides who answers. An allowlist rather than a denylist because litellm's keyword surface
    #: is wide, aliased and moves between releases - `base_url` alone overrides `api_base` inside
    #: litellm, so a denylist naming `api_base` never saw it, and an operator holding only
    #: `change_llmmodel` could redirect a call and send the provider's key with it, against rule
    #: L3. `timeout` stays: it is the registry's own default, beaten by a call that states one.
    #: The service layer filters against this tuple again immediately before the call, because a
    #: fixture, a migration or a direct ORM write never runs `clean()`.
    #:
    #: The list covers the providers the registry offers, not one of them. `top_k` is Anthropic's
    #: and every local model's; `extra_body` is how litellm carries a vLLM-specific parameter it
    #: has no name for; `reasoning_effort` is how a reasoning model is told how hard to think.
    #: Leaving them out did not make the field safer - none of them decides who answers - it made
    #: a legitimate row unsavable, including the edit that unticks *Enabled*.
    ALLOWED_PARAMETERS = (
        "extra_body",
        "frequency_penalty",
        "logit_bias",
        "n",
        "presence_penalty",
        "reasoning_effort",
        "seed",
        "stop",
        "temperature",
        "timeout",
        "top_k",
        "top_p",
    )

    natural_key_field_names = ["provider", "name"]

    class Meta:
        """Meta class."""

        ordering = ["provider__name", "name"]
        verbose_name = "LLM Model"
        verbose_name_plural = "LLM Models"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "name"],
                name="event_tracker_llmmodel_provider_name_unique",
            ),
        ]

    def __str__(self):
        """Stringify instance."""
        return f"{self.provider.name}: {self.name}"

    def clean(self):
        """Refuse every parameter the service layer owns, keeping the generation parameters alone.

        Caught here rather than at call time: a credential or an endpoint in this field would
        already have been written to a change-logged row and served over REST and GraphQL by the
        time a call read it, which is what rule L3 exists to prevent. A duplicated call argument
        would surface as a failed model call rather than as the configuration mistake it is.
        """
        super().clean()
        parameters = self.default_parameters or {}

        # The one allowed key whose *value* matters here. A stored `null` or `0` is not a request
        # for no limit; it is a row that says nothing, and rule L6 promises every call carries a
        # timeout. The service layer falls through such a value rather than trusting it, and this
        # stops it being written in the first place.
        timeout = parameters.get("timeout", 1)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValidationError(
                {"default_parameters": f"timeout must be a positive number of seconds, got {timeout!r}."}
            )

        offenders = sorted(key for key in parameters if key not in self.ALLOWED_PARAMETERS)
        if offenders:
            raise ValidationError(
                {
                    "default_parameters": (
                        f"{', '.join(offenders)} cannot be set here. This field carries generation parameters "
                        f"only ({', '.join(self.ALLOWED_PARAMETERS)}); the endpoint and the credentials come "
                        "from the provider's external integration, and the model and messages come from the "
                        "call itself."
                    )
                }
            )


class LLMUsageRecord(BaseModel):
    """The accounting row for one LLM call, successful or not.

    Deliberately neither a PrimaryModel nor change-logged, for the IngestionStats reason: a busy
    consumer writes one of these per surviving event, and change logging them would write an
    ObjectChange per model call and bury the change log under a record of bookkeeping.

    Written only by `services.llm` (rule L1). No UI or API route offers a write method.
    """

    model = models.ForeignKey(
        to=LLMModel,
        on_delete=models.PROTECT,
        related_name="usage_records",
    )
    ticket = models.ForeignKey(
        to=EventTicket,
        on_delete=models.SET_NULL,
        related_name="llm_usage",
        null=True,
        blank=True,
        help_text="The ticket this call was about, when there was one. Spend history survives ticket deletion.",
    )
    purpose = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=LLMPurposeChoices,
        db_index=True,
        help_text="What the call was for.",
    )
    request_id = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        blank=True,
        help_text="The provider's response identifier, for finding the call in provider-side logs.",
    )
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=0,
        help_text="USD, computed from the model's registered costs and the provider's reported usage.",
    )
    latency_ms = models.PositiveIntegerField(default=0)
    success = models.BooleanField(db_index=True)
    error = models.TextField(blank=True, help_text="Why the call failed, when it did. Capped by the service.")
    called_at = models.DateTimeField(default=timezone.now, db_index=True)

    # An accounting row is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["-called_at"]
        get_latest_by = "called_at"
        verbose_name = "LLM Usage Record"
        verbose_name_plural = "LLM Usage Records"

    def __str__(self):
        """Stringify instance."""
        return f"{self.purpose} call at {self.called_at:%Y-%m-%d %H:%M:%S}"


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class MCPServer(PrimaryModel):  # pylint: disable=too-many-ancestors
    """An MCP server the app may reach, registered by an operator (ADR 0007).

    Carries no credentials of its own: the ExternalIntegration it points at holds the endpoint URL,
    its headers and its TLS settings, and that integration's secrets group holds whatever
    authenticates to it. The same arrangement `LLMProvider` uses, read by the same code shape.

    Registering a server makes it known. It does not make anything callable: every tool arrives
    disabled and stays that way until somebody enables it (rule M4).
    """

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    external_integration = models.ForeignKey(
        to="extras.ExternalIntegration",
        on_delete=models.PROTECT,
        related_name="mcp_servers",
        help_text="Carries the streamable HTTP endpoint, its headers and TLS settings, and its secrets group.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="A disabled server refuses every tool call before any network traffic (rule M4).",
    )
    last_discovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this server's tool list was last read. Discovery never enables anything (rule M5).",
    )

    class Meta:
        """Meta class."""

        ordering = ["name"]
        verbose_name = "MCP Server"
        verbose_name_plural = "MCP Servers"

    def __str__(self):
        """Stringify instance."""
        return self.name

    def clean(self):
        """A server with no URL is a server nothing can reach.

        Checked here rather than left to the first call: an integration is a shared object, and the
        one being pointed at may have been made for something that did not need a remote URL.
        """
        super().clean()
        if self.external_integration_id is not None and not self.external_integration.remote_url:
            raise ValidationError(
                {"external_integration": "An MCP server needs an external integration with a remote URL."}
            )


@extras_features("custom_links", "custom_validators", "export_templates", "graphql", "webhooks")
class MCPTool(PrimaryModel):  # pylint: disable=too-many-ancestors
    """One tool a server advertises, and whether an operator has allowed it (ADR 0007).

    Both booleans below default the unhelpful way on purpose, and that is the whole security
    argument of this model: a server advertising forty tools grants access to none of them, and a
    tool nobody has classified is treated as though it changes the network.
    """

    server = models.ForeignKey(
        to=MCPServer,
        on_delete=models.CASCADE,
        related_name="tools",
        help_text="A tool cannot outlive the server that offers it.",
    )
    name = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text="The tool name sent on the wire.",
    )
    description = models.TextField(blank=True, help_text="As the server advertised it.")
    input_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text="The JSON Schema the server advertised for this tool's arguments.",
    )
    mutating = models.BooleanField(
        default=True,
        help_text=(
            "Whether calling this tool changes something. A mutating tool never runs without a "
            "human approving that call (rule M6). True until a person says otherwise: guessing "
            "wrong this way costs a click, and guessing wrong the other way changes the network. "
            "Only a person sets this. Discovery never does, whatever the server claims."
        ),
    )
    advertised_read_only = models.BooleanField(
        null=True,
        blank=True,
        help_text=(
            "What the server's own readOnlyHint annotation claims, or unset when it claims "
            "nothing. Shown so a reviewer can see it; never used to decide anything. The MCP "
            "specification says in as many words that a client must not make tool-use decisions "
            "from annotations it received from the server they describe."
        ),
    )
    enabled = models.BooleanField(
        default=False,
        help_text="Disabled means uncallable, whatever a model asks for (rule M4). Discovery never enables.",
    )
    definition_fingerprint = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        blank=True,
        help_text=(
            "Digest of everything the server advertised about this tool - its description as well "
            "as its argument schema - as of the last discovery. A change under an enabled tool "
            "disables it (rule M5). The description is in the digest because it is half of what a "
            "reviewer read, and because it becomes the tool's semantics in an agent's prompt."
        ),
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When discovery last saw this tool advertised. An older time means the server stopped offering it.",
    )

    natural_key_field_names = ["server", "name"]

    class Meta:
        """Meta class."""

        ordering = ["server__name", "name"]
        verbose_name = "MCP Tool"
        verbose_name_plural = "MCP Tools"
        constraints = [
            models.UniqueConstraint(
                fields=["server", "name"],
                name="event_tracker_mcptool_server_name_unique",
            ),
        ]

    def __str__(self):
        """Stringify instance."""
        return f"{self.server.name}: {self.name}"

    @property
    def is_callable(self):
        """Whether a call to this tool could reach the server at all (rule M4).

        Read by the service layer before any network traffic, and by the UI to explain why a tool
        an operator enabled is still not being offered to a model.
        """
        return self.enabled and self.server.enabled

    @property
    def claims_read_only(self):
        """Whether the server says this tool only reads, and this deployment disagrees.

        The pair worth showing a reviewer: a tool the server calls read-only which nobody has
        classified that way. It is a prompt to look, never an argument to believe.
        """
        return self.advertised_read_only is True and self.mutating


class AgentRun(BaseModel):
    """One pass of the agent loop over one ticket (ADR 0009).

    Deliberately neither a PrimaryModel nor change-logged, for the IngestionStats and
    LLMUsageRecord reason: it records what happened, not what anyone meant. What anyone meant is
    on the ticket's own trail, where a person looks.

    Written only by `services.agent` (section 10's guard). No UI or API route offers a write
    method, and `waiting_approval` is a finished run rather than a blocked one: the loop ends at
    the gate and hands its worker slot back.
    """

    ticket = models.ForeignKey(
        to=EventTicket,
        on_delete=models.CASCADE,
        related_name="agent_runs",
        help_text="A run is about exactly one ticket.",
    )
    status = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=AgentRunStatusChoices,
        default=AgentRunStatusChoices.RUNNING,
        db_index=True,
    )
    started_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="agent_runs",
        null=True,
        blank=True,
        help_text=(
            "The person who launched this run. Not the actor: everything the model decided is "
            "recorded on the ticket as 'ai' with no user (rule S4)."
        ),
    )
    job_result = models.ForeignKey(
        to="extras.JobResult",
        on_delete=models.SET_NULL,
        related_name="event_tracker_agent_runs",
        null=True,
        blank=True,
        help_text="The Nautobot-side record of the same run, when one launched it.",
    )
    parent = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="resumptions",
        null=True,
        blank=True,
        help_text="The run this one resumed after a person approved its proposal.",
    )
    transcript = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Every message, tool call and tool result, in the order the model saw them (rule A7). "
            "Resumption replays it, and a person asking why the model wanted to do that reads it."
        ),
    )
    iterations = models.PositiveIntegerField(default=0, help_text="Model calls this run spent.")
    error = models.TextField(blank=True, help_text="Why the run failed, when it did. Capped by the service.")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # A record of what happened is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["-started_at"]
        get_latest_by = "started_at"
        verbose_name = "Agent Run"
        verbose_name_plural = "Agent Runs"

    def __str__(self):
        """Stringify instance."""
        return f"Agent run on {self.ticket_id} ({self.status})"

    @property
    def is_live(self):
        """Whether this run is still part of the ticket's current chain (rule A9)."""
        return self.status in AGENT_RUN_LIVE_STATUSES


class AgentToolCall(BaseModel):
    """One tool call an agent asked for, and what became of it.

    Written only by `services.agent` and `services.mcp`. Every call is on the record before its
    caller sees the answer (rule M7), which is the same promise rule L1 makes about model calls.
    """

    run = models.ForeignKey(
        to=AgentRun,
        on_delete=models.CASCADE,
        related_name="tool_calls",
    )
    tool = models.ForeignKey(
        to=MCPTool,
        on_delete=models.PROTECT,
        related_name="calls",
        help_text="PROTECT, so the record of a call outlives a tidy-up of the registry.",
    )
    arguments = models.JSONField(
        default=dict,
        blank=True,
        help_text="What the model asked for, as it asked. Frozen at proposal: approving approves these (7.3).",
    )
    status = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=AgentToolCallStatusChoices,
        default=AgentToolCallStatusChoices.PROPOSED,
        db_index=True,
    )
    tool_fingerprint = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        blank=True,
        help_text=(
            "The tool's definition digest when this call was proposed. Re-checked before the call "
            "runs (rule M6): what was approved was a call on the tool as it read then."
        ),
    )
    decided_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="agent_tool_decisions",
        null=True,
        blank=True,
        help_text="Who approved or denied this call. A decision without a person is refused (7.3).",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True, help_text="What came back, capped by the service (rule M8).")
    error = models.TextField(blank=True, help_text="Why the call failed, when it did. Capped by the service.")
    latency_ms = models.PositiveIntegerField(default=0)
    proposed_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="When the model asked for this call. A BaseModel has no timestamps of its own.",
    )
    called_at = models.DateTimeField(null=True, blank=True, help_text="When the call actually reached the server.")

    # A record of what happened is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["proposed_at"]
        get_latest_by = "proposed_at"
        verbose_name = "Agent Tool Call"
        verbose_name_plural = "Agent Tool Calls"
        # Deciding is its own permission, and `change_agenttoolcall` does not imply it (7.3).
        # Approving a call against the network is not the same right as editing a row.
        permissions = [("approve_agenttoolcall", "Can approve or deny a proposed agent tool call")]

    def __str__(self):
        """Stringify instance."""
        return f"{self.tool} ({self.status})"

    @property
    def awaits_decision(self):
        """Whether somebody still has to approve or deny this call."""
        return self.status == AgentToolCallStatusChoices.PROPOSED

    @property
    def is_decided(self):
        """Whether a decision has already been made. A call is decided once, and only once (7.3)."""
        return self.decided_at is not None or self.status != AgentToolCallStatusChoices.PROPOSED


class TicketEmbedding(BaseModel):
    """One closed ticket, as a vector (Phase 5A).

    Deliberately neither a PrimaryModel nor change-logged, for the IngestionStats and AgentRun
    reason: it is derived data. Re-indexing a ticket would otherwise file an ObjectChange recording
    that a number changed.

    Written only by `services.rag` (rule R1's neighbour in section 10's guards). No UI or API route
    offers a write method.

    A OneToOne rather than a foreign key, because rule R4 is "one embedding per ticket" and the
    database should hold that rather than the service remembering to.
    """

    ticket = models.OneToOneField(
        to=EventTicket,
        on_delete=models.CASCADE,
        related_name="embedding",
    )
    embedding = VectorField(
        # No fixed width. A `vector(768)` column would bake one embedding model's dimension into
        # the schema, so changing model would mean a migration and a rewrite of every row - and
        # rule R2 already guarantees comparisons happen within one model, hence within one
        # dimension. What was actually stored is recorded in `dimensions` below.
        dimensions=None,
        help_text="The vector itself. Only ever compared with vectors from the same model (rule R2).",
    )
    document = models.TextField(
        help_text=(
            "Exactly what was embedded. Stored so a person can see why two tickets matched - a "
            "similarity score with no visible input is not something anybody can check."
        ),
    )
    model = models.ForeignKey(
        to=LLMModel,
        on_delete=models.PROTECT,
        related_name="embeddings",
        help_text="Which model produced it. A vector is comparable only with its own model's (R2).",
    )
    dimensions = models.PositiveIntegerField(
        help_text="Denormalised from the model, so a mismatch is detectable without a join.",
    )
    document_fingerprint = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text=(
            "Digest of the document. Re-closing a ticket whose document did not change costs no "
            "model call and no write."
        ),
    )
    indexed_at = models.DateTimeField(default=timezone.now, db_index=True)

    # Derived data is identified by nothing but itself.
    natural_key_field_names = ["pk"]

    class Meta:
        """Meta class."""

        ordering = ["-indexed_at"]
        get_latest_by = "indexed_at"
        verbose_name = "Ticket Embedding"
        verbose_name_plural = "Ticket Embeddings"

    def __str__(self):
        """Stringify instance."""
        return f"Embedding of {self.ticket_id}"
