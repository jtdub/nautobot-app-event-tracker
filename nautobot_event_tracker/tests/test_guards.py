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


def _python_files_outside_services():
    """Yield every app module that is not part of the service layer."""
    for path in sorted(APP_ROOT.rglob("*.py")):
        if SERVICES_DIR in path.parents or path == SERVICES_DIR:
            continue
        if MIGRATIONS_DIR in path.parents:
            continue
        yield path


def _manager_call_offenders(paths, models, methods):
    """Yield `path:line` for every `<Model>.objects.<method>()` call in these files.

    The AST rather than a string search: a docstring mentioning the call is not the call. Shared by
    both write guards so a fix to the matcher cannot land in one copy only.
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
        offenders = list(_manager_call_offenders(paths, {"TicketUpdate"}, {"create"}))

        # Direct construction, which the manager matcher cannot see.
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TicketUpdate":
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")

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


class IngestionGuardTest(SimpleTestCase):
    """Phase 2's two rules, asserted rather than trusted.

    The ingestion package writes tickets only through the service layer, and calls no language
    model. Both are the kind of rule a well-meaning change breaks silently.
    """

    #: Anything that would mean an LLM had arrived in a phase that is meant to have none.
    FORBIDDEN_IMPORTS = ("litellm", "openai", "anthropic", "langchain", "transformers")

    def _ingestion_modules(self):
        """Every module in the ingestion package and the consumer command."""
        ingestion = APP_ROOT / "ingestion"
        command = APP_ROOT / "management" / "commands" / "eventconsumer.py"
        return sorted(ingestion.rglob("*.py")) + [command]

    def test_no_direct_ticket_writes_in_the_ingestion_package(self):
        """`EventTicket.objects.create()` there would bypass the trail and the dedup rule."""
        offenders = list(
            _manager_call_offenders(
                self._ingestion_modules(),
                {"EventTicket", "TicketUpdate"},
                {"create", "get_or_create", "update_or_create", "bulk_create"},
            )
        )

        self.assertEqual(
            offenders,
            [],
            "Ingestion must write tickets through services/tickets.py. Offending lines: " + ", ".join(offenders),
        )

    def test_the_ingestion_package_imports_no_language_model(self):
        """Phase 2 is AI-free by design; triage is Phase 3 and plugs in at a named seam."""
        offenders = []
        for path in self._ingestion_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] in self.FORBIDDEN_IMPORTS:
                        offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno} imports {name}")

        self.assertEqual(
            offenders,
            [],
            "No module under ingestion/ may import a language model client. Offending lines: " + ", ".join(offenders),
        )

    def test_the_app_declares_no_language_model_dependency(self):
        """The same rule at the packaging level, where it is equally easy to break."""
        pyproject = (APP_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
        dependencies = pyproject.split("[tool.poetry.group.dev.dependencies]")[0]
        for name in self.FORBIDDEN_IMPORTS:
            self.assertNotIn(name, dependencies, f"'{name}' must not be a runtime dependency in Phase 2")
