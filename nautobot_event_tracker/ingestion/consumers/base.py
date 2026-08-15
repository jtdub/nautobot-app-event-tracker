"""The broker interface, and the credential handling every implementation shares.

Small on purpose. Per ADR 0004 the interface expresses only what both brokers can honestly do, and
declares what one of them cannot: `supports_replay` is a property a caller can read rather than a
guarantee it has to assume.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerMessage:
    """One message as the broker handed it over.

    `timestamp` is the broker's, not the payload's. Normalization prefers what the event itself
    says happened; this is the fallback for an event that does not say.
    """

    topic: str
    value: bytes
    key: str = None
    offset: int = None
    timestamp: datetime = None


class EventConsumer(ABC):
    """A source of broker messages."""

    #: Whether a consumer that dies can resume where it stopped. Read it; do not assume it.
    supports_replay = False

    #: Which block of the ingestion configuration holds this implementation's connection settings.
    settings_key = ""

    def __init__(self, *, settings, topics):
        """Hold the settings and the topics, without connecting to anything yet."""
        self.settings = settings
        self.topics = tuple(topics)

    @abstractmethod
    def connect(self):
        """Open the connection and subscribe."""

    @abstractmethod
    def poll(self, timeout):
        """Return the next message, or None if none arrived within `timeout` seconds.

        Returning on a timeout rather than blocking forever is what lets the loop notice a
        shutdown signal, flush its counters and roll a stats bucket while no traffic is arriving.
        """

    @abstractmethod
    def acknowledge(self, message):
        """Tell the broker this message is done with.

        Called only after the ticket transaction has committed, which is what makes redelivery -
        rather than loss - the failure mode of a crash mid-message.
        """

    @abstractmethod
    def close(self):
        """Release the connection."""

    def __enter__(self):
        """Connect on the way in."""
        self.connect()
        return self

    def __exit__(self, *exception):
        """Close on the way out, whatever happened."""
        self.close()
        return False


def connection_details(settings, *, url_key):
    """Resolve a broker address and its credentials.

    An `external_integration` names a Nautobot object holding the address and a secrets group, so
    that credentials are rotated where every other credential in the deployment is rotated. Falling
    back to a plain URL in settings suits a lab and nothing else.

    Returns `(url, username, password)`, either of the last two being None when not configured.
    """
    name = settings.get("external_integration")
    if not name:
        return settings.get(url_key), None, None

    from nautobot.extras.models import ExternalIntegration  # pylint: disable=import-outside-toplevel

    try:
        integration = ExternalIntegration.objects.get(name=name)
    except ObjectDoesNotExist as error:
        raise ImproperlyConfigured(f"External integration '{name}' does not exist.") from error

    return (integration.remote_url, *_credentials(integration))


def _credentials(integration):
    """Read the username and password out of an integration's secrets group, if it has one."""
    from nautobot.extras.choices import (  # pylint: disable=import-outside-toplevel
        SecretsGroupAccessTypeChoices,
        SecretsGroupSecretTypeChoices,
    )
    from nautobot.extras.secrets.exceptions import SecretError  # pylint: disable=import-outside-toplevel

    if integration.secrets_group is None:
        return None, None

    def secret(secret_type):
        """One secret, or None when the group does not carry it."""
        try:
            return integration.secrets_group.get_secret_value(
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=secret_type,
                obj=integration,
            )
        except (SecretError, ObjectDoesNotExist):
            return None

    return secret(SecretsGroupSecretTypeChoices.TYPE_USERNAME), secret(SecretsGroupSecretTypeChoices.TYPE_PASSWORD)
