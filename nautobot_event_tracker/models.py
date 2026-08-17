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

from nautobot_event_tracker.choices import (
    ATTACHMENT_UPDATE_TYPES,
    TERMINAL_STATUSES,
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


@extras_features("graphql")
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


@extras_features("graphql")
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
        """An OpenAI-compatible endpoint is unreachable without a URL to reach it at."""
        super().clean()
        if (
            self.provider_type == LLMProviderTypeChoices.OPENAI_COMPATIBLE
            and self.external_integration_id is not None
            and not self.external_integration.remote_url
        ):
            raise ValidationError(
                {
                    "external_integration": (
                        "An OpenAI-compatible provider needs an external integration with a remote URL."
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
        help_text="Extra request parameters (temperature and friends), passed through on every call.",
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


@extras_features("graphql")
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
