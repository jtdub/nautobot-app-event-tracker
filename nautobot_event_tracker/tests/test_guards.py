"""Cross-cutting guards that belong to no single model.

These are the mechanical half of two rules that are otherwise only conventions: that the service
layer is the sole writer of ticket status (ADR 0001), and that the app ships no page templates
(ADR 0008). Both are the kind of rule a well-meaning future change breaks silently, so they are
asserted rather than trusted.
"""

import ast
import tokenize
from pathlib import Path

from django.test import SimpleTestCase

APP_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = APP_ROOT / "services"
MIGRATIONS_DIR = APP_ROOT / "migrations"

#: Every manager method that writes. One set for every sole-writer guard, so strengthening the
#: matcher strengthens all of them at once rather than the one someone happened to be editing.
WRITE_METHODS = frozenset({"create", "get_or_create", "update_or_create", "bulk_create", "update"})


def _python_files_outside_services():
    """Yield every app module that is not part of the service layer."""
    for path in sorted(APP_ROOT.rglob("*.py")):
        if SERVICES_DIR in path.parents or path == SERVICES_DIR:
            continue
        if MIGRATIONS_DIR in path.parents:
            continue
        yield path


def _manager_call_offenders(paths, models, methods=WRITE_METHODS):
    """Yield `path:line` for every `<Model>.objects.<method>()` call in these files.

    The AST rather than a string search: a docstring mentioning the call is not the call. Shared by
    every write guard so a fix to the matcher cannot land in one copy only.
    """
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = node.func.value
            if (
                node.func.attr in methods
                and isinstance(value, ast.Attribute)
                and value.attr == "objects"
                and isinstance(value.value, ast.Name)
                and value.value.id in models
            ):
                yield f"{path.relative_to(APP_ROOT)}:{node.lineno}"


def _constructor_call_offenders(paths, class_names):
    """Yield `path:line` for every direct `<Model>(...)` construction in these files.

    The companion to `_manager_call_offenders`, shared for the same reason: the sole-writer
    guards need both shapes, and two copies of a matcher drift.
    """
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in class_names:
                yield f"{path.relative_to(APP_ROOT)}:{node.lineno}"


class StatusAssignmentGuardTest(SimpleTestCase):
    """No module outside services/ may assign to a ticket's status."""

    def test_no_status_assignment_outside_the_service_layer(self):
        """Walk the AST looking for `<something>.status = ...` assignments.

        A string search would trip over docstrings and comments; the AST only sees real code.
        Tests are included in the sweep deliberately: a fixture that sets status directly is
        exactly the violation this rule exists to prevent.
        """
        offenders = []
        for path in _python_files_outside_services():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "status":
                        offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")

        self.assertEqual(
            offenders,
            [],
            "Ticket status must only be assigned inside services/tickets.py. Offending lines: " + ", ".join(offenders),
        )

    def test_no_direct_ticketupdate_creation_outside_the_service_layer(self):
        """Only the service layer may create TicketUpdate rows.

        The model test suite is exempt: it has to exercise the append-only guard directly, which
        means constructing rows without going through a service function.
        """
        allowed = {"tests/test_models.py"}
        paths = [path for path in _python_files_outside_services() if str(path.relative_to(APP_ROOT)) not in allowed]
        offenders = list(_manager_call_offenders(paths, {"TicketUpdate"}))
        offenders += list(_constructor_call_offenders(paths, {"TicketUpdate"}))

        self.assertEqual(
            offenders,
            [],
            "TicketUpdate rows must only be created by the service layer. Offending lines: " + ", ".join(offenders),
        )


class StringLiteralGuardTest(SimpleTestCase):
    """No two string literals may sit adjacent on one line.

    Python silently joins them, which reads exactly like a forgotten comma in an argument list.
    The formatter creates these by collapsing a wrapped string that now fits on one line, so they
    appear without anyone typing them. pylint reports it as implicit-str-concat, but only on some
    versions - this caught a CI failure that the local pylint had passed.
    """

    def test_no_adjacent_string_literals_on_one_line(self):
        """Scan the token stream; the AST cannot see the join because the parser has done it."""
        offenders = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            with open(path, "rb") as handle:
                tokens = list(tokenize.tokenize(handle.readline))
            previous = None
            for token in tokens:
                if token.type == tokenize.STRING:
                    if previous is not None and previous.end[0] == token.start[0]:
                        offenders.append(f"{path.relative_to(APP_ROOT)}:{token.start[0]}")
                    previous = token
                elif token.type not in (tokenize.NL, tokenize.COMMENT):
                    previous = None

        self.assertEqual(
            offenders,
            [],
            "Adjacent string literals on one line read as a missing comma. Join them into a "
            "single literal. Offending lines: " + ", ".join(offenders),
        )


class SerializerAndFormGuardTest(SimpleTestCase):
    """The API and the UI must not expose service-owned fields as writable."""

    def test_serializer_marks_service_owned_fields_read_only(self):
        """Every service-owned field must be in read_only_fields."""
        from nautobot_event_tracker.api.serializers import (  # pylint: disable=import-outside-toplevel
            SERVICE_OWNED_FIELDS,
            EventTicketSerializer,
        )

        read_only = set(EventTicketSerializer.Meta.read_only_fields)
        for field in SERVICE_OWNED_FIELDS:
            self.assertIn(field, read_only, f"'{field}' must be read-only on the ticket serializer")

    def test_ticket_forms_omit_service_owned_fields(self):
        """Neither the edit form nor the bulk edit form may reach them."""
        from nautobot_event_tracker.api.serializers import (  # pylint: disable=import-outside-toplevel
            SERVICE_OWNED_FIELDS,
        )
        from nautobot_event_tracker.forms import (  # pylint: disable=import-outside-toplevel
            EventTicketBulkEditForm,
            EventTicketForm,
        )

        for form_class in (EventTicketForm, EventTicketBulkEditForm):
            declared = set(getattr(form_class.Meta, "fields", []) or [])
            for field in SERVICE_OWNED_FIELDS:
                self.assertNotIn(
                    field,
                    declared,
                    f"'{field}' must not be settable through {form_class.__name__}",
                )


class TemplateGuardTest(SimpleTestCase):
    """ADR 0008: the app ships no hand-written page templates."""

    def test_no_templates_directory(self):
        """A templates/ directory in the app would mean the UI framework was bypassed."""
        templates_dir = APP_ROOT / "templates"
        self.assertFalse(
            templates_dir.exists(),
            "The app must not ship page templates; build the UI from the UI Component Framework.",
        )

    def test_no_html_files_in_the_app_package(self):
        """Catch a stray template placed somewhere other than templates/."""
        html_files = [path.relative_to(APP_ROOT) for path in APP_ROOT.rglob("*.html") if "static" not in path.parts]
        self.assertEqual([str(path) for path in html_files], [])


def _is_forbidden_package(name, forbidden):
    """Whether this distribution or module name belongs to a forbidden project.

    Matches the project's companion packages too - `langchain_core` and `langchain-community`
    are langchain - because a banned SDK that arrives under a suffixed name is the same SDK.
    Hyphens and underscores are the same character for this purpose: one spells the
    distribution, the other the module.
    """
    stem = name.split(".")[0].replace("-", "_")
    return any(stem == entry or stem.startswith(f"{entry}_") for entry in forbidden)


def _import_offenders(paths, forbidden):
    """Yield `path:line imports name` for every import of a forbidden package in these files.

    Walks the whole AST, so an import buried in a function body is found too. Shared by the
    import guards so a fix to the matcher cannot land in one copy only.
    """
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if _is_forbidden_package(name, forbidden):
                    yield f"{path.relative_to(APP_ROOT)}:{node.lineno} imports {name}"


class IngestionGuardTest(SimpleTestCase):
    """Phase 2's two rules, asserted rather than trusted.

    The ingestion package writes tickets only through the service layer, and calls no language
    model. Both are the kind of rule a well-meaning change breaks silently. Phase 3 narrowed the
    second rule rather than removing it: litellm now exists, but only behind `services/llm.py`,
    and ingestion still talks to a model exclusively through that service (rule L2).
    """

    #: SDKs that must appear nowhere: every call goes through litellm (ADR 0006).
    PROVIDER_SDKS = ("openai", "anthropic", "langchain", "transformers")

    #: The one LLM client the app uses, importable only where `test_litellm_is_imported_only_in_the_llm_service`
    #: allows.
    LLM_LIBRARY = "litellm"

    def _ingestion_modules(self):
        """Every module in the ingestion package and the consumer command."""
        ingestion = APP_ROOT / "ingestion"
        command = APP_ROOT / "management" / "commands" / "eventconsumer.py"
        return sorted(ingestion.rglob("*.py")) + [command]

    def test_no_direct_ticket_writes_in_the_ingestion_package(self):
        """`EventTicket.objects.create()` there would bypass the trail and the dedup rule."""
        offenders = list(_manager_call_offenders(self._ingestion_modules(), {"EventTicket", "TicketUpdate"}))

        self.assertEqual(
            offenders,
            [],
            "Ingestion must write tickets through services/tickets.py. Offending lines: " + ", ".join(offenders),
        )

    def test_the_ingestion_package_imports_no_language_model(self):
        """Ingestion reaches a model only through `services.llm`, never through a client library."""
        forbidden = self.PROVIDER_SDKS + (self.LLM_LIBRARY,)
        offenders = list(_import_offenders(self._ingestion_modules(), forbidden))

        self.assertEqual(
            offenders,
            [],
            "No module under ingestion/ may import a language model client. Offending lines: " + ", ".join(offenders),
        )

    def test_litellm_is_imported_only_in_the_llm_service(self):
        """L2 - one import site for litellm, and no provider SDK anywhere in the app."""
        allowed = {"services/llm.py"}
        paths = [path for path in sorted(APP_ROOT.rglob("*.py")) if str(path.relative_to(APP_ROOT)) not in allowed]
        offenders = list(_import_offenders(paths, self.PROVIDER_SDKS + (self.LLM_LIBRARY,)))
        offenders += list(_import_offenders([APP_ROOT / "services" / "llm.py"], self.PROVIDER_SDKS))

        self.assertEqual(
            offenders,
            [],
            "litellm belongs in services/llm.py alone, and provider SDKs belong nowhere. "
            "Offending lines: " + ", ".join(offenders),
        )

    @staticmethod
    def _poetry():
        """The parsed `[tool.poetry]` table, so the guards read data rather than source strings."""
        try:
            import tomllib  # pylint: disable=import-outside-toplevel
        except ImportError:  # Python 3.10
            import tomli as tomllib  # pylint: disable=import-outside-toplevel

        with open(APP_ROOT.parent / "pyproject.toml", "rb") as handle:
            return tomllib.load(handle)["tool"]["poetry"]

    def test_the_app_declares_no_provider_sdk_dependency(self):
        """The no-SDK rule at the packaging level, where it is equally easy to break.

        Matched by project rather than by exact name: `langchain-core` is langchain, and an
        exact-key check would wave it through.
        """
        offenders = [name for name in self._poetry()["dependencies"] if _is_forbidden_package(name, self.PROVIDER_SDKS)]
        self.assertEqual(
            offenders,
            [],
            "Provider SDKs must not be runtime dependencies; every call goes through litellm. "
            "Offending dependencies: " + ", ".join(offenders),
        )

    def test_litellm_is_an_optional_dependency(self):
        """litellm stays behind the `llm` extra: a deployment without triage installs no client."""
        poetry = self._poetry()
        self.assertIs(
            poetry["dependencies"].get("litellm", {}).get("optional"),
            True,
            "litellm must be a runtime dependency marked optional",
        )
        self.assertIn("litellm", poetry["extras"].get("llm", ()), "the 'llm' extra must install litellm")
        self.assertIn("litellm", poetry["extras"].get("all", ()), "the 'all' extra must include litellm")


class LLMUsageGuardTest(SimpleTestCase):
    """Rule L1's mechanical half: only `services/llm.py` writes usage records."""

    def test_no_direct_llm_usage_record_writes_outside_the_service_layer(self):
        """The model test suite is exempt, as for TicketUpdate, and for the same reason."""
        allowed = {"tests/test_models.py"}
        paths = [path for path in _python_files_outside_services() if str(path.relative_to(APP_ROOT)) not in allowed]
        offenders = list(_manager_call_offenders(paths, {"LLMUsageRecord"}))
        offenders += list(_constructor_call_offenders(paths, {"LLMUsageRecord"}))

        self.assertEqual(
            offenders,
            [],
            "LLMUsageRecord rows must only be written by services/llm.py. Offending lines: " + ", ".join(offenders),
        )


def _write_call_offenders(paths, methods):
    """Yield `path:line calls .<method>()` for every write-shaped method call in these files.

    Broader than `_manager_call_offenders`, which matches `<Model>.objects.create()` and needs to
    know the model's name. This one knows nothing and refuses everything, which is what a module
    that must not write at all wants: a `save()` on an instance it happens to be holding is as much
    a write as a manager call, and neither belongs in a reader.
    """
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in methods:
                yield f"{path.relative_to(APP_ROOT)}:{node.lineno} calls .{node.func.attr}()"


def _app_import_offenders(paths, prefix):
    """Yield `path:line imports <module>` for every import of an app package under `prefix`."""
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name == prefix or name.startswith(f"{prefix}."):
                    yield f"{path.relative_to(APP_ROOT)}:{node.lineno} imports {name}"


class EnrichmentGuardTest(SimpleTestCase):
    """Phase 4A's rule E3, in both halves: the resolver reads, and services does not import ingestion."""

    #: Every method that writes, whatever it is called on. `update` is here at the cost of a dict's
    #: own `update()`: a reader that wants to merge a dictionary can assign items instead, and the
    #: rule is worth more than the convenience.
    WRITE_CALLS = WRITE_METHODS | {"save", "delete", "bulk_update"}

    def test_the_enrichment_resolver_writes_nothing(self):
        """E3 - it finds objects and hands them back; `services/tickets.py` does the writing."""
        offenders = list(_write_call_offenders([SERVICES_DIR / "enrichment.py"], self.WRITE_CALLS))

        self.assertEqual(
            offenders,
            [],
            "services/enrichment.py must only read. Offending lines: " + ", ".join(offenders),
        )

    def test_the_service_layer_imports_nothing_from_ingestion(self):
        """The dependency runs one way: `ingestion` imports `services`, never the reverse.

        A rule the codebase has always followed and never checked. Phase 4A is the first phase to
        give anyone a reason to break it - the resolver is called by the pipeline and is about
        payloads - so it is the right moment to assert it.
        """
        offenders = list(_app_import_offenders(sorted(SERVICES_DIR.rglob("*.py")), "nautobot_event_tracker.ingestion"))

        self.assertEqual(
            offenders,
            [],
            "services/ must not import the ingestion package. Offending lines: " + ", ".join(offenders),
        )


class MCPGuardTest(SimpleTestCase):
    """ADR 0007's two mechanical halves: one client, and no way to run a process."""

    #: The MCP client library and the HTTP client it is driven with, importable only in
    #: `services/mcp.py` (rule M1).
    MCP_LIBRARIES = ("mcp", "httpx2")

    #: Every way Python starts a process. ADR 0007 refused the stdio transport because it means
    #: process execution driven by database rows; this is that decision, asserted rather than
    #: trusted to nobody reaching for the convenience later.
    PROCESS_MODULES = ("subprocess", "pty", "multiprocessing")
    PROCESS_CALLS = frozenset({"system", "popen", "fork", "execv", "execvp", "execve", "spawnv", "posix_spawn"})

    def test_the_mcp_client_is_imported_only_in_the_mcp_service(self):
        """M1 - one import site, the same arrangement litellm has."""
        allowed = {"services/mcp.py"}
        paths = [path for path in sorted(APP_ROOT.rglob("*.py")) if str(path.relative_to(APP_ROOT)) not in allowed]
        offenders = list(_import_offenders(paths, self.MCP_LIBRARIES))

        self.assertEqual(
            offenders,
            [],
            "The MCP client belongs in services/mcp.py alone. Offending lines: " + ", ".join(offenders),
        )

    def test_no_module_can_start_a_process(self):
        """M2 - streamable HTTP only, and nothing anywhere that could run a local command."""
        offenders = list(_import_offenders(sorted(APP_ROOT.rglob("*.py")), self.PROCESS_MODULES))
        offenders += list(_write_call_offenders(sorted(APP_ROOT.rglob("*.py")), self.PROCESS_CALLS))

        self.assertEqual(
            offenders,
            [],
            "No module may start a process: ADR 0007 refused stdio for exactly this reason. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_the_mcp_client_is_an_optional_dependency(self):
        """A deployment that registers no server installs no MCP client.

        Both packages, because `services/mcp.py` imports both: relying on `httpx2` arriving as one
        of `mcp`'s own dependencies makes the day that changes look like a missing `mcp` extra.
        """
        poetry = IngestionGuardTest._poetry()  # pylint: disable=protected-access
        for package in self.MCP_LIBRARIES:
            self.assertIs(
                poetry["dependencies"].get(package, {}).get("optional"),
                True,
                f"{package} must be a runtime dependency marked optional",
            )
            self.assertIn(package, poetry["extras"].get("mcp", ()), f"the 'mcp' extra must install {package}")
            self.assertIn(package, poetry["extras"].get("all", ()), f"the 'all' extra must include {package}")


class AgentGuardTest(SimpleTestCase):
    """Phase 4B section 10: the agent's rules, asserted rather than trusted.

    The MCP guards are next door and already cover the two ADR 0007 halves - one client library,
    and no way to start a process. These are the three this phase adds.
    """

    #: The fixtures build runs and calls directly, because a fixture that went through the service
    #: would need a model call to produce a row; the model suite constructs them to exercise the
    #: model itself. The same exemption `TicketUpdate` and `LLMUsageRecord` have, for the same
    #: reason.
    ALLOWED = {"tests/fixtures.py", "tests/test_models.py"}

    def _paths(self):
        """Every app module outside `services/`, minus the two exempt test modules."""
        return [
            path for path in _python_files_outside_services() if str(path.relative_to(APP_ROOT)) not in self.ALLOWED
        ]

    def test_agent_records_are_written_only_by_the_service_layer(self):
        """A run and a tool call are records of what a service did, like every other row of the kind."""
        offenders = list(_manager_call_offenders(self._paths(), {"AgentRun", "AgentToolCall"}))
        offenders += list(_constructor_call_offenders(self._paths(), {"AgentRun", "AgentToolCall"}))

        self.assertEqual(
            offenders,
            [],
            "AgentRun and AgentToolCall rows must only be written under services/. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_the_agent_service_writes_no_ticket_update_directly(self):
        """The sole-writer guard exempts everything under services/, so this names the module.

        The gate's three trail entries belong in `services/tickets.py` with every other one: they
        are `TicketUpdate` rows, and ADR 0001 is about who writes those, not about which package
        the caller happens to live in.
        """
        agent = SERVICES_DIR / "agent.py"
        offenders = list(_manager_call_offenders([agent], {"TicketUpdate"}))
        offenders += list(_constructor_call_offenders([agent], {"TicketUpdate"}))

        self.assertEqual(
            offenders,
            [],
            "services/agent.py must write the ticket trail through services/tickets.py. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_the_agent_service_imports_no_language_model_or_mcp_client(self):
        """A5's sibling at the import level: the agent reaches both through their services.

        Covered by the two one-import-site guards already, and asserted here as well because this
        is the module that would most plausibly reach for either directly - it is the one that
        wants a model and a tool in the same function.
        """
        forbidden = IngestionGuardTest.PROVIDER_SDKS + (IngestionGuardTest.LLM_LIBRARY,) + MCPGuardTest.MCP_LIBRARIES
        offenders = list(_import_offenders([SERVICES_DIR / "agent.py"], forbidden))

        self.assertEqual(
            offenders,
            [],
            "services/agent.py reaches a model through services/llm.py and a tool through "
            "services/mcp.py. Offending lines: " + ", ".join(offenders),
        )
