"""Backfill the retrieval corpus with closed tickets."""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from nautobot_event_tracker.choices import TicketStatusChoices
from nautobot_event_tracker.models import EventTicket
from nautobot_event_tracker.services import rag as rag_service
from nautobot_event_tracker.services.exceptions import LLMError


class Command(BaseCommand):
    """Index closed tickets that are not in the corpus yet, or all of them again.

    Two occasions want this. The first is turning retrieval on in a deployment that has been
    closing tickets for a year: the Job Hook only sees closes from now on, and the year of answers
    already written down is the corpus worth having. The second is changing embedding model, after
    which the old vectors are still there and are simply never compared with the new ones
    (rule R2) - the panel goes quiet rather than going wrong, and `--reindex` is how it comes back.
    """

    help = "Embed closed tickets into the retrieval corpus."

    def add_arguments(self, parser):
        """One ticket, a bounded batch, or everything."""
        parser.add_argument(
            "--limit",
            type=int,
            help="Stop after this many tickets. Useful for trying it on a few before the lot.",
        )
        parser.add_argument(
            "--reindex",
            action="store_true",
            help="Re-embed tickets that already have an embedding. Needed after changing model.",
        )

    def handle(self, *args, **options):
        """Index, reporting every failure rather than only the first."""
        try:
            settings = rag_service.get_settings()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        if not settings.enabled:
            raise CommandError(
                "Retrieval is not enabled. Set rag.enabled in PLUGINS_CONFIG, with a provider and "
                "an embedding model, before indexing anything."
            )

        tickets = EventTicket.objects.filter(status=TicketStatusChoices.CLOSED).order_by("closed_at")
        if not options["reindex"]:
            tickets = tickets.filter(embedding__isnull=True)
        if options["limit"]:
            tickets = tickets[: options["limit"]]

        indexed = skipped = failed = 0
        for ticket in tickets:
            try:
                result = rag_service.index_ticket(ticket)
            except (LLMError, ImproperlyConfigured) as error:
                # Reported and carried on. One unembeddable ticket must not stop a backfill of
                # nine hundred, and an operator wants the whole list rather than the first name.
                failed += 1
                self.stderr.write(self.style.WARNING(f"{ticket.pk}: {error}"))
                continue
            if result is None:
                skipped += 1
            else:
                indexed += 1

        self.stdout.write(f"Indexed {indexed}, skipped {skipped}, failed {failed}.")
        if failed:
            raise CommandError(f"{failed} ticket(s) could not be indexed; see above.")
