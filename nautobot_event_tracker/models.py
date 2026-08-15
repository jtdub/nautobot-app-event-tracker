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
        return self.status not in (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED)

    def clean(self):
        """Validate shape, never actor. Actor rules live in the service layer (ADR 0001)."""
        super().clean()
        errors = {}

        # C1 - timestamps agree with status.
        expects_resolved_at = self.status in (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED)
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
        expects_resolution = self.status in (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED)
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

    def clean(self):  # pylint: disable=too-many-branches
        """C3 - an update's payload and actor must match its type."""
        super().clean()
        errors = {}

        if self.update_type == UpdateTypeChoices.COMMENT and not self.message:
            errors["message"] = "A comment must have a message."

        if self.update_type == UpdateTypeChoices.STATUS_CHANGE:
            if not self.from_status:
                errors["from_status"] = "A status change must record the status it moved from."
            if not self.to_status:
                errors["to_status"] = "A status change must record the status it moved to."
            if self.from_status and self.to_status and self.from_status == self.to_status:
                errors["to_status"] = "A status change must move between two different statuses."
        else:
            if self.from_status:
                errors["from_status"] = f"An update of type '{self.update_type}' must not record a from status."
            if self.to_status:
                errors["to_status"] = f"An update of type '{self.update_type}' must not record a to status."

        attachment_types = (UpdateTypeChoices.OBJECT_ATTACHED, UpdateTypeChoices.OBJECT_DETACHED)
        if self.update_type in attachment_types:
            if self.related_object_type is None:
                errors["related_object_type"] = "An attachment update must record the related object type."
            if self.related_object_id is None:
                errors["related_object_id"] = "An attachment update must record the related object ID."
        else:
            if self.related_object_type is not None:
                errors["related_object_type"] = (
                    f"An update of type '{self.update_type}' must not record a related object type."
                )
            if self.related_object_id is not None:
                errors["related_object_id"] = (
                    f"An update of type '{self.update_type}' must not record a related object ID."
                )

        if self.source == TicketSourceChoices.HUMAN and self.user is None:
            errors["user"] = "An update from a human must record the acting user."
        if self.source in (TicketSourceChoices.AI, TicketSourceChoices.SYSTEM) and self.user is not None:
            errors["user"] = f"An update from '{self.source}' must not record an acting user."

        if errors:
            raise ValidationError(errors)

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
