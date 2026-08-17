"""Exceptions raised by the service layer."""


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


class LLMError(Exception):
    """Base class for every error the LLM service raises. See rule L4.

    No litellm or provider exception type escapes `services.llm`; callers catch this one family.
    When the failure happened after a call was attempted, `record` carries the LLMUsageRecord that
    was written for it (rule L1); a refusal before any network traffic carries None.
    """

    def __init__(self, message, *, record=None):
        """Keep the usage record, when there is one, next to the error it explains."""
        super().__init__(message)
        self.record = record


class LLMConfigurationError(LLMError):
    """The provider or model is missing, disabled, or not callable as configured. See rule L8."""


class LLMCallError(LLMError):
    """The call left the process and failed: a timeout, a refusal, or a provider error."""


class LLMResponseError(LLMError):
    """The call returned, but what came back is not usable."""
