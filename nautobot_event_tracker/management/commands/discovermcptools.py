"""Read the registered MCP servers' tool lists and reconcile the registry with them.

The same `services.mcp.discover()` the server's detail page calls, for a deployment that would
rather run it on a schedule than press a button. It grants nothing either way (rule M5): new tools
arrive disabled, and a tool whose schema changed under an approval is disabled again and named in
the output.
"""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from nautobot_event_tracker.models import MCPServer
from nautobot_event_tracker.services import mcp as mcp_service
from nautobot_event_tracker.services.exceptions import MCPError


class Command(BaseCommand):
    """Discover the tools on one MCP server, or on every enabled one."""

    help = __doc__

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument("--server", help="Discover only the server of this name. Default: every enabled server.")

    def handle(self, *args, **options):
        """Discover each server in turn, reporting every failure rather than the first.

        One unreachable server must not stop the others: a deployment with four servers and one
        outage should come back with three servers reconciled and one named, not with nothing done.
        """
        servers = self._servers(options.get("server"))
        try:
            # Resolved once, before anything is attempted. A missing extra is an
            # `ImproperlyConfigured`, deliberately outside the family the loop below handles, so
            # without this a scheduled run on a deployment installed without `[mcp]` reports a
            # traceback where the documentation promises a sentence.
            mcp_service.require_client()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error

        failures = []

        for server in servers:
            try:
                report = mcp_service.discover(server)
            except MCPError as error:
                failures.append(f"{server}: {error}")
                self.stderr.write(self.style.ERROR(f"{server}: {error}"))
                continue

            self.stdout.write(f"{server}: {report.summary()}")
            if report.needs_attention:
                # Named rather than counted: these are the rows somebody has to go and decide
                # about, and a number does not tell them which.
                self.stdout.write(
                    self.style.WARNING(
                        "  disabled and needing review: " + ", ".join(tool.name for tool in report.needs_attention)
                    )
                )

        if failures:
            raise CommandError(f"{len(failures)} of {len(servers)} servers could not be discovered.")

        self.stdout.write(self.style.SUCCESS(f"Discovered {len(servers)} server(s)."))

    @staticmethod
    def _servers(name):
        """The servers this run covers, or a refusal naming what is wrong."""
        if name:
            try:
                server = MCPServer.objects.get(name=name)
            except MCPServer.DoesNotExist as error:
                raise CommandError(f"No MCP server named '{name}' exists.") from error
            if not server.enabled:
                raise CommandError(f"MCP server '{server}' is disabled.")
            return [server]

        servers = list(MCPServer.objects.filter(enabled=True))
        if not servers:
            raise CommandError("No enabled MCP servers are registered.")
        return servers
