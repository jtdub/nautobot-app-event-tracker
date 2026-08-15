"""Exceptions raised by the ticket service layer."""


class TicketServiceError(Exception):
    """Base class for every error the ticket service raises."""


class TicketImmutableError(TicketServiceError):
    """The acting source may not modify this ticket in its current state.

    Raised when an AI actor attempts any mutation of a resolved or closed ticket. See rule S3.
    """


class InvalidTransitionError(TicketServiceError):
    """The requested status change is not an edge in the workflow graph. See rule S2."""


class InvalidActorError(TicketServiceError):
    """The source and user arguments are not a valid combination. See rule S4."""
