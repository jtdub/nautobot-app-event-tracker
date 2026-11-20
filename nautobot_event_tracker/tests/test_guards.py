"""Cross-cutting guards that belong to no single model.

These are the mechanical half of two rules that are otherwise only conventions: that the service
layer is the sole writer of ticket status (ADR 0001), and that the app ships no page templates
(ADR 0008). Both are the kind of rule a well-meaning future change breaks silently, so they are
asserted rather than trusted.
"""

import ast
import importlib
import re
import tokenize
import tomllib
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
    """ADR 0008: the app ships one page template, and this is the list of it.

    The rule was "no templates at all" for four phases. Phase 5B needed a page with no object,
    which Nautobot's UI Component Framework cannot render on its own - a `Tab` asks the object for
    its own URL before deciding whether to draw - and core ships no template that takes a bare list
    of panels. So the allowlist is one file, and it exists to keep the count at one: the risk ADR
    0008 is about is an app that drifts into hand-written pages, and that starts with a second
    template rather than with the first.
    """

    #: Every template the app is allowed to ship, relative to the app package. Adding to this is a
    #: decision about ADR 0008 rather than a test fix, and the ADR says so.
    ALLOWED_TEMPLATES = {"templates/nautobot_event_tracker/dashboard.html"}

    def test_the_templates_directory_holds_only_the_allowed_files(self):
        """Everything under `templates/`, not just the `.html` - a `.txt` is a template too."""
        templates = APP_ROOT / "templates"
        shipped = sorted(str(path.relative_to(APP_ROOT)) for path in templates.rglob("*") if path.is_file())

        self.assertEqual(shipped, sorted(self.ALLOWED_TEMPLATES))

    def test_no_stray_html_elsewhere_in_the_app_package(self):
        """Catch a template placed somewhere other than `templates/`."""
        html_files = sorted(
            str(path.relative_to(APP_ROOT)) for path in APP_ROOT.rglob("*.html") if "static" not in path.parts
        )

        self.assertEqual(html_files, sorted(self.ALLOWED_TEMPLATES))

    #: Markup the UI Component Framework or a core layout template would otherwise have emitted.
    #: An allowed template may hold a form and a block wrapper; the moment it holds a grid or a
    #: table it has started reimplementing the thing ADR 0008 says to delegate to.
    FRAMEWORK_MARKUP = ("<table", '<div class="row"', '<div class="col-')

    def test_the_allowed_template_extends_nothing_but_the_base(self):
        """The hazard ADR 0008 names is coupling to core's page internals, not having a file.

        A template that extends `generic/object_retrieve.html` inherits every change core makes to
        it. One that extends `base.html` and calls `render_components` inherits the framework's
        panels instead, which is the whole point of the exception.
        """
        for name in sorted(self.ALLOWED_TEMPLATES):
            with self.subTest(template=name):
                body = (APP_ROOT / name).read_text(encoding="utf-8")
                extends = [line for line in body.splitlines() if "{% extends" in line]
                self.assertEqual(extends, ['{% extends "base.html" %}'])

    def test_the_allowed_template_draws_no_layout_of_its_own(self):
        """The claim the ADR amendment actually makes, asserted rather than trusted.

        Extending `base.html` is necessary and not sufficient: the first version of this template
        passed that check while containing a private copy of core's `two_over_one.html` grid, which
        is exactly the drift - every other page in the deployment moves when core changes its
        layout, and the copy does not.
        """
        for name in sorted(self.ALLOWED_TEMPLATES):
            with self.subTest(template=name):
                body = (APP_ROOT / name).read_text(encoding="utf-8")
                offenders = [markup for markup in self.FRAMEWORK_MARKUP if markup in body]

                self.assertEqual(
                    offenders,
                    [],
                    f"{name} draws layout the framework owns; include core's template instead. "
                    "Found: " + ", ".join(offenders),
                )


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


def _pyproject():
    """Return the parsed pyproject.toml."""
    with open(APP_ROOT.parent / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


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
        return _pyproject()["tool"]["poetry"]

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


#: The two test modules exempt from every record-model sole-writer guard. The fixtures build rows
#: directly, because a fixture that went through the service would need a model call to produce
#: one; the model suite constructs them to exercise the model itself.
RECORD_WRITE_EXEMPT = frozenset({"tests/fixtures.py", "tests/test_models.py"})

FIXTURES_MODULE = "tests/fixtures.py"
"""The one module that `AIRegistryGuardTest` exempts."""


def _record_writer_paths():
    """Every app module outside `services/`, minus the test modules those guards exempt.

    Lifted out of `AgentGuardTest` and `RagGuardTest`, which each carried a copy. Phase 5B would
    have made a third, which is the point at which two copies become a helper.
    """
    return [
        path for path in _python_files_outside_services() if str(path.relative_to(APP_ROOT)) not in RECORD_WRITE_EXEMPT
    ]


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

    def test_agent_records_are_written_only_by_the_service_layer(self):
        """A run and a tool call are records of what a service did, like every other row of the kind."""
        offenders = list(_manager_call_offenders(_record_writer_paths(), {"AgentRun", "AgentToolCall"}))
        offenders += list(_constructor_call_offenders(_record_writer_paths(), {"AgentRun", "AgentToolCall"}))

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


class RagGuardTest(SimpleTestCase):
    """Phase 5A section 10: the three rules that are otherwise only conventions."""

    def test_nothing_retrieved_can_reach_a_prompt(self):
        """R9, and the only half of it that survives somebody deciding it would be nice to try.

        The corpus is built from payloads written by whoever can put a line on a consumed topic.
        One ticket being read by a model was already true; indexing changes the reach, because a
        document that steers one investigation becomes one that can be retrieved for every future
        ticket resembling it. So the two modules that build prompts may not import the module that
        does retrieval - not "should not", cannot.

        If this test ever fails, the right response is almost certainly to revert the import rather
        than to add an exemption. Section 9 of the Phase 5A spec is the argument.
        """
        prompt_builders = [
            APP_ROOT / "ingestion" / "triage.py",
            SERVICES_DIR / "agent.py",
        ]
        offenders = list(_app_import_offenders(prompt_builders, "nautobot_event_tracker.services.rag"))

        self.assertEqual(
            offenders,
            [],
            "Retrieved text must never reach a prompt: the modules that build prompts may not "
            "import services/rag.py. Offending lines: " + ", ".join(offenders),
        )

    def test_the_pgvector_query_surface_is_only_in_the_rag_service(self):
        """R1 - searching lives in one module.

        `models.py` is exempt and has to be: `TicketEmbedding` uses `VectorField`, and a Django
        model cannot import a field type lazily. Migrations are exempt for the same reason and are
        generated besides. That is the field *definition*; what this guards is everything else -
        the distance functions and the query construction.
        """
        allowed = {"services/rag.py", "models.py"}
        paths = [
            path
            for path in sorted(APP_ROOT.rglob("*.py"))
            # Migrations are generated, and one that adds a vector column has to name its type.
            if MIGRATIONS_DIR not in path.parents and str(path.relative_to(APP_ROOT)) not in allowed
        ]
        offenders = list(_import_offenders(paths, ("pgvector",)))

        self.assertEqual(
            offenders,
            [],
            "pgvector belongs in services/rag.py, apart from the field type in models.py. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_embeddings_are_written_only_by_the_service_layer(self):
        """A corpus row is derived data, like every other record model in this app."""
        offenders = list(_manager_call_offenders(_record_writer_paths(), {"TicketEmbedding"}))
        offenders += list(_constructor_call_offenders(_record_writer_paths(), {"TicketEmbedding"}))

        self.assertEqual(
            offenders,
            [],
            "TicketEmbedding rows must only be written under services/. Offending lines: " + ", ".join(offenders),
        )


class AnalyticsGuardTest(SimpleTestCase):
    """Phase 5B section 9: the dashboard reads, calls no model, and always knows who is asking.

    The first phase whose guards are entirely about a module's *absences*. There is no new writer
    to constrain, because there is no new writer at all.
    """

    ANALYTICS = SERVICES_DIR / "analytics.py"

    #: The two modules a dashboard would most plausibly reach for, and the one that would let it
    #: summarize its own charts. Rule D7 forecloses "describe this month's incidents", which is a
    #: different phase and a different argument.
    FORBIDDEN_SERVICES = (
        "nautobot_event_tracker.services.llm",
        "nautobot_event_tracker.services.agent",
        "nautobot_event_tracker.services.rag",
    )

    #: The public functions that touch no queryset, so they have nothing to restrict. Every other
    #: public name in the module answers "how many" about somebody's rows.
    WITHOUT_A_USER = frozenset({"get_settings", "is_enabled", "window_choices"})

    #: The four panel groups. These take `user` keyword-only and without a default, so no caller
    #: can supply one positionally by accident or leave it out and get the estate's numbers.
    PANEL_FUNCTIONS = frozenset({"ingestion_health", "ticket_flow", "model_cost", "agent_activity"})

    def _public_functions(self):
        """Every module-level public function in `services/analytics.py`, as AST nodes."""
        tree = ast.parse(self.ANALYTICS.read_text(encoding="utf-8"), filename=str(self.ANALYTICS))
        return [node for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")]

    def test_the_dashboard_writes_nothing(self):
        """D1 - the first phase in this app that adds no writer, asserted rather than promised.

        The same sweep the enrichment resolver gets, and for the same reason: a `save()` on an
        instance the module happens to be holding is as much a write as a manager call. A cache
        table is the obvious thing to reach for here, and this is what refuses it.
        """
        offenders = list(_write_call_offenders([self.ANALYTICS], EnrichmentGuardTest.WRITE_CALLS))

        self.assertEqual(
            offenders,
            [],
            "services/analytics.py must only read. Offending lines: " + ", ".join(offenders),
        )

    def test_the_dashboard_calls_no_model(self):
        """D7 - the page shows numbers a person reads; it does not narrate them.

        `services/llm.py` is the import that matters. The other two are here because each of them
        would bring it along, and an indirect model call is a model call.
        """
        offenders = []
        for prefix in self.FORBIDDEN_SERVICES:
            offenders += list(_app_import_offenders([self.ANALYTICS], prefix))

        self.assertEqual(
            offenders,
            [],
            "The dashboard describes what happened; it does not narrate it with a model. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_every_analytics_function_takes_a_user(self):
        """D2 stated as a property of the module rather than of a panel.

        An aggregate leaks without returning anything: a count of 4,812 open tickets tells a user
        scoped out of that estate exactly how large it is. Phase 5A wrote R6 about a panel and left
        three other surfaces unguarded; this is that lesson, asserted at the only place it can be -
        the signature, where there is no way to ask without saying who is asking.
        """
        offenders = []
        for node in self._public_functions():
            if node.name in self.WITHOUT_A_USER:
                continue
            arguments = [argument.arg for argument in node.args.args + node.args.kwonlyargs]
            if "user" not in arguments:
                offenders.append(f"services/analytics.py:{node.lineno} {node.name}()")

        self.assertEqual(
            offenders,
            [],
            "Every public analytics function must take `user`. Offending lines: " + ", ".join(offenders),
        )

    def test_the_panel_functions_take_a_user_keyword_only_and_without_a_default(self):
        """The other half of D2: not optional, not defaulted, not `None` for "internal use".

        There is no internal use. A default would be the one call site that quietly aggregates over
        everything, and it would look exactly like every other call.
        """
        offenders = []
        for node in self._public_functions():
            if node.name not in self.PANEL_FUNCTIONS:
                continue
            keyword_only = [argument.arg for argument in node.args.kwonlyargs]
            if "user" not in keyword_only:
                offenders.append(f"services/analytics.py:{node.lineno} {node.name}() takes user positionally")
                continue
            default = node.args.kw_defaults[keyword_only.index("user")]
            if default is not None:
                offenders.append(f"services/analytics.py:{node.lineno} {node.name}() defaults user")

        self.assertEqual(
            offenders,
            [],
            "The panel functions must take `user` keyword-only and without a default. "
            "Offending lines: " + ", ".join(offenders),
        )


class AIRegistryGuardTest(SimpleTestCase):
    """ADR 0010: the AI registry belongs to another app, and this one does not grow a second copy.

    Both halves of the decision are the kind that erodes quietly. A registry model reappears here
    the first time somebody needs one field the other app does not have - which is exactly how
    this app came to have four of them - and the dependency drifts to optional the first time
    somebody wants a lighter install. The second would be worse than untidy: `models.py` holds
    foreign keys into that app, so a deployment without it does not degrade, it fails to start.
    """

    DELETED_MODELS = frozenset({"LLMProvider", "LLMModel", "MCPServer", "MCPTool"})
    """The four models that this app deleted (ADR 0010)."""

    REGISTRY_MODELS = frozenset({"AIProvider", "AIModel", "MCPServer", "MCPTool"})
    """The four models that the AI Models app owns."""

    REGISTRY_DISTRIBUTION = "nautobot-ai-models"
    REGISTRY_MODULE = "nautobot_ai_models"

    def test_the_app_defines_no_registry_model(self):
        """No class in `models.py` takes a deleted name or a registry name."""
        tree = ast.parse((APP_ROOT / "models.py").read_text(encoding="utf-8"), filename="models.py")
        offenders = [
            f"models.py:{node.lineno} defines {node.name}"
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name in self.DELETED_MODELS | self.REGISTRY_MODELS
        ]

        self.assertEqual(
            offenders,
            [],
            "These names are deleted or belong to the AI Models app (ADR 0010). Do not define them here. "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_the_registry_app_is_a_required_dependency(self):
        """Not an extra, and not optional.

        litellm and the MCP client are lazy because they are only ever called, and a deployment
        that calls neither installs neither. This one is not comparable: `models.py` declares
        foreign keys into it, so Django resolves them at import time on every start.
        """
        poetry = IngestionGuardTest._poetry()  # pylint: disable=protected-access
        declared = poetry["dependencies"].get(self.REGISTRY_DISTRIBUTION)

        self.assertIsNotNone(declared, f"{self.REGISTRY_DISTRIBUTION} must be a runtime dependency")
        self.assertIsNot(
            declared.get("optional") if isinstance(declared, dict) else False,
            True,
            f"{self.REGISTRY_DISTRIBUTION} must not be optional: this app cannot start without it",
        )
        for extra, members in poetry.get("extras", {}).items():
            self.assertNotIn(
                self.REGISTRY_DISTRIBUTION,
                members,
                f"{self.REGISTRY_DISTRIBUTION} must not sit behind the '{extra}' extra",
            )

    def test_the_registry_app_is_not_written_by_this_one(self):
        """This app reads the registry. Writing it is the other app's job, and its Jobs' job.

        A tool this app enabled, or a model it created, is a row an operator did not review and
        cannot find the provenance of. The one thing that may write those tables is the discovery
        the AI Models app ships. `tests/fixtures.py` is exempt, because no discovery Job runs in a
        test database.
        """
        paths = [path for path in sorted(APP_ROOT.rglob("*.py")) if str(path.relative_to(APP_ROOT)) != FIXTURES_MODULE]
        offenders = list(_manager_call_offenders(paths, self.REGISTRY_MODELS))
        offenders += list(_constructor_call_offenders(paths, self.REGISTRY_MODELS))

        self.assertEqual(
            offenders,
            [],
            "Registry rows are written by the AI Models app, never by this one (ADR 0010). "
            "Offending lines: " + ", ".join(offenders),
        )

    def test_the_write_guard_names_each_registry_model(self):
        """Each name that the write guard sweeps for is a class of the AI Models app."""
        registry_models = importlib.import_module(f"{self.REGISTRY_MODULE}.models")

        for name in sorted(self.REGISTRY_MODELS):
            self.assertTrue(isinstance(getattr(registry_models, name, None), type), f"{name} is not a registry class")


class PythonSupportGuardTest(SimpleTestCase):
    """The declared Python range, the classifiers and the ruff target agree."""

    @staticmethod
    def _python_range():
        """Return the floor and the ceiling minor versions of the declared Python range."""
        constraint = _pyproject()["tool"]["poetry"]["dependencies"]["python"]
        match = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", constraint)
        if match is None:
            raise AssertionError(f"The Python constraint {constraint!r} is not in the form '>=3.X,<3.Y'.")
        return int(match.group(1)), int(match.group(2))

    def test_the_python_floor_is_3_11_or_later(self):
        """The floor is a Python release that its maker still supports and that litellm 1.97.0 can run on."""
        floor, _ceiling = self._python_range()

        self.assertGreaterEqual(floor, 11)

    def test_the_classifiers_match_the_python_range(self):
        """Each supported minor version has one classifier, and no other minor version has one."""
        floor, ceiling = self._python_range()
        prefix = "Programming Language :: Python :: 3."

        classifiers = _pyproject()["tool"]["poetry"]["classifiers"]

        declared = [
            name.removeprefix("Programming Language :: Python :: ") for name in classifiers if name.startswith(prefix)
        ]
        self.assertEqual(declared, [f"3.{minor}" for minor in range(floor, ceiling)])

    def test_ruff_targets_the_python_floor(self):
        """Ruff checks the code against the oldest Python that the app supports."""
        floor, _ceiling = self._python_range()

        self.assertEqual(_pyproject()["tool"]["ruff"]["target-version"], f"py3{floor}")

    def test_no_backport_of_tomllib_is_declared(self):
        """Python 3.11 has tomllib, so no group declares the tomli backport."""
        poetry = _pyproject()["tool"]["poetry"]
        tables = [poetry["dependencies"]] + [group["dependencies"] for group in poetry["group"].values()]

        self.assertEqual([table for table in tables if "tomli" in table], [])
