"""Test the Event Tracker filtersets."""

from nautobot.apps.testing import FilterTestCases

from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMProviderTypeChoices,
    SeverityChoices,
    TicketSourceChoices,
    TicketStatusChoices,
)
from nautobot_event_tracker.filters import (
    AgentRunFilterSet,
    AgentToolCallFilterSet,
    EventTicketFilterSet,
    EventTypeFilterSet,
    LLMModelFilterSet,
    LLMProviderFilterSet,
    LLMUsageRecordFilterSet,
    MCPServerFilterSet,
    MCPToolFilterSet,
    TicketUpdateFilterSet,
)
from nautobot_event_tracker.models import (
    AgentRun,
    AgentToolCall,
    EventTicket,
    EventType,
    LLMModel,
    LLMProvider,
    LLMUsageRecord,
    MCPServer,
    MCPTool,
    TicketUpdate,
)
from nautobot_event_tracker.services import tickets as ticket_service
from nautobot_event_tracker.tests import fixtures


class EventTypeFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for EventType."""

    queryset = EventType.objects.all()
    filterset = EventTypeFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_event_types()

    def test_q_matches_name(self):
        """Search matches the name."""
        params = {"q": "Interface"}
        self.assertTrue(self.filterset(params, self.queryset).qs.filter(name__contains="Interface").exists())

    def test_q_matches_description(self):
        """Search also matches the description."""
        EventType.objects.create(name="Searchable", description="a very distinctive phrase")
        params = {"q": "distinctive"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_name(self):
        """Exact name filter."""
        params = {"name": ["Test Interface Down"]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_default_severity(self):
        """Severity filter."""
        params = {"default_severity": [SeverityChoices.CRITICAL]}
        self.assertTrue(self.filterset(params, self.queryset).qs.exists())

    def test_enabled(self):
        """Enabled flag filter, both ways."""
        self.assertFalse(self.filterset({"enabled": False}, self.queryset).qs.filter(enabled=True).exists())
        self.assertFalse(self.filterset({"enabled": True}, self.queryset).qs.filter(enabled=False).exists())


class EventTicketFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for EventTicket."""

    queryset = EventTicket.objects.all()
    filterset = EventTicketFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create tickets across several statuses, severities and assignments."""
        cls.user = fixtures.create_user()
        cls.other_user = fixtures.create_user("someone-else")
        cls.event_types = fixtures.create_event_types()
        cls.location = fixtures.create_location()

        cls.new_ticket = fixtures.create_ticket(
            user=cls.user, event_type=cls.event_types[0], title="Alpha ticket", dedup_key="alpha"
        )
        cls.resolved_ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.RESOLVED, user=cls.user, title="Beta ticket"
        )
        cls.closed_ticket = fixtures.create_ticket_in_status(
            TicketStatusChoices.CLOSED, user=cls.user, title="Gamma ticket"
        )

        ticket_service.assign(
            ticket=cls.new_ticket,
            assignee=cls.other_user,
            source=TicketSourceChoices.HUMAN,
            user=cls.user,
        )
        ticket_service.attach_object(
            ticket=cls.new_ticket,
            obj=cls.location,
            source=TicketSourceChoices.HUMAN,
            user=cls.user,
        )

    def _filter(self, params):
        return self.filterset(params, self.queryset).qs

    def test_q_matches_title(self):
        """Search matches the title."""
        self.assertEqual(self._filter({"q": "Alpha"}).count(), 1)

    def test_q_matches_event_type_name(self):
        """Search reaches through to the event type name."""
        self.assertTrue(self._filter({"q": "Interface"}).exists())

    def test_status(self):
        """Status filter accepts multiple values."""
        results = self._filter({"status": [TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED]})
        self.assertEqual(results.count(), 2)

    def test_severity(self):
        """Severity filter."""
        self.assertTrue(self._filter({"severity": [SeverityChoices.MAJOR]}).exists())

    def test_source(self):
        """Source filter."""
        self.assertEqual(self._filter({"source": [TicketSourceChoices.HUMAN]}).count(), 3)
        self.assertEqual(self._filter({"source": [TicketSourceChoices.AI]}).count(), 0)

    def test_event_type_by_name(self):
        """The natural key filter takes a name."""
        self.assertTrue(self._filter({"event_type": ["Test Interface Down"]}).exists())

    def test_assigned_to_by_username(self):
        """Assignment filter takes a username."""
        results = self._filter({"assigned_to": ["someone-else"]})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().pk, self.new_ticket.pk)

    def test_has_assignee(self):
        """Membership filter, both directions."""
        self.assertEqual(self._filter({"has_assignee": True}).count(), 1)
        self.assertEqual(self._filter({"has_assignee": False}).count(), 2)

    def test_is_open(self):
        """is_open must agree with the terminal status set."""
        self.assertEqual(self._filter({"is_open": True}).count(), 1)
        self.assertEqual(self._filter({"is_open": False}).count(), 2)

    def test_dedup_key(self):
        """Dedup key filter."""
        self.assertEqual(self._filter({"dedup_key": ["alpha"]}).count(), 1)

    def test_event_count(self):
        """Event count filter."""
        self.assertEqual(self._filter({"event_count": [1]}).count(), 3)

    def test_last_seen(self):
        """Date filters are wired up."""
        self.assertTrue(self._filter({"last_seen__gte": ["2000-01-01T00:00:00Z"]}).exists())

    def test_resolved_at_and_closed_at(self):
        """Terminal timestamps are filterable."""
        self.assertEqual(self._filter({"resolved_at__gte": ["2000-01-01T00:00:00Z"]}).count(), 2)
        self.assertEqual(self._filter({"closed_at__gte": ["2000-01-01T00:00:00Z"]}).count(), 1)

    def test_related_object_type_matches_attached(self):
        """A ticket with a location attached matches the location content type."""
        results = self._filter({"related_object_type": ["dcim.location"]})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().pk, self.new_ticket.pk)

    def test_related_object_type_excludes_detached(self):
        """An object attached and then detached must stop matching.

        This is the case a plain join would get wrong, which is why the filter replays the trail.
        """
        ticket_service.detach_object(
            ticket=self.new_ticket,
            obj=self.location,
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )
        self.assertEqual(self._filter({"related_object_type": ["dcim.location"]}).count(), 0)


class TicketUpdateFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for TicketUpdate."""

    queryset = TicketUpdate.objects.all()
    filterset = TicketUpdateFilterSet

    @classmethod
    def setUpTestData(cls):
        """Create a ticket with a varied trail."""
        # `actor`, not `user`: Nautobot's TestCase.setUp() assigns `self.user` its own logged-in
        # test user, which would shadow a class attribute of that name.
        cls.actor = fixtures.create_user()
        fixtures.create_event_types()
        cls.ticket = fixtures.create_ticket(user=cls.actor)
        ticket_service.add_comment(
            ticket=cls.ticket,
            message="a distinctive human comment",
            source=TicketSourceChoices.HUMAN,
            user=cls.actor,
        )
        ticket_service.add_comment(ticket=cls.ticket, message="an AI note", source=TicketSourceChoices.AI)

    def _filter(self, params):
        return self.filterset(params, self.queryset).qs

    def test_q_filter_valid(self):
        """Not applicable: the generic version edits a row and saves it.

        `TicketUpdate.save()` refuses that outright - the model is append-only, which is the point
        of ADR 0001's audit trail. The `q` filter is covered by `test_q_matches_message` instead.
        """
        self.skipTest("TicketUpdate is append-only; the generic q-filter test rewrites a row.")

    def test_q_matches_message(self):
        """Search matches the message body."""
        self.assertEqual(self._filter({"q": "distinctive"}).count(), 1)

    def test_ticket(self):
        """Filter by ticket."""
        self.assertEqual(self._filter({"ticket": [self.ticket.pk]}).count(), 3)

    def test_update_type(self):
        """Filter by update type."""
        self.assertEqual(self._filter({"update_type": ["comment"]}).count(), 2)

    def test_source(self):
        """Filter by source."""
        self.assertEqual(self._filter({"source": [TicketSourceChoices.AI]}).count(), 1)

    def test_user_by_username(self):
        """Filter by acting username; AI rows have no user and must not match."""
        results = self._filter({"user": [self.actor.username]})
        self.assertTrue(results.exists())
        self.assertFalse(results.filter(source=TicketSourceChoices.AI).exists())


class LLMProviderFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for LLMProvider."""

    queryset = LLMProvider.objects.all()
    filterset = LLMProviderFilterSet
    generic_filter_tests = (
        ["name"],
        ["description"],
    )

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_llmprovider(name="Local Lab", description="the on-prem endpoint")
        fixtures.create_llmprovider(name="Disabled Provider", description="switched off", enabled=False)
        fixtures.create_llmprovider(
            name="Spare Provider",
            description="a third, for the generic suite",
            provider_type=LLMProviderTypeChoices.ANTHROPIC,
        )

    def test_q_matches_name_and_description(self):
        """Search covers both text fields."""
        self.assertEqual(self.filterset({"q": "Lab"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "on-prem"}, self.queryset).qs.count(), 1)

    def test_enabled(self):
        """Enabled flag filter, both ways."""
        self.assertFalse(self.filterset({"enabled": False}, self.queryset).qs.filter(enabled=True).exists())
        self.assertFalse(self.filterset({"enabled": True}, self.queryset).qs.filter(enabled=False).exists())

    def test_provider_type(self):
        """Type filter, asserted against a queryset that holds more than one type.

        The counts have to differ from the unfiltered total, or the assertion passes just as well
        when the filter matches everything.
        """
        params = {"provider_type": [LLMProviderTypeChoices.OPENAI_COMPATIBLE]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {"provider_type": [LLMProviderTypeChoices.ANTHROPIC]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
        self.assertEqual(self.queryset.count(), 3)


class LLMModelFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for LLMModel."""

    queryset = LLMModel.objects.all()
    filterset = LLMModelFilterSet
    generic_filter_tests = (
        ["name"],
        ["provider", "provider__name"],
    )

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        cls.provider = fixtures.create_llmprovider()
        other = fixtures.create_llmprovider(name="Other Provider")
        spare = fixtures.create_llmprovider(name="Spare Provider")
        fixtures.create_llmmodel(name="fast-model", provider=cls.provider)
        fixtures.create_llmmodel(name="smart-model", provider=other)
        fixtures.create_llmmodel(name="spare-model", provider=spare)

    def test_provider_by_name_or_pk(self):
        """The provider filter takes the natural key or the ID."""
        self.assertEqual(self.filterset({"provider": ["Test Provider"]}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"provider": [str(self.provider.pk)]}, self.queryset).qs.count(), 1)

    def test_q_matches_the_provider_name_too(self):
        """Searching by where a model lives is as natural as by what it is called."""
        self.assertEqual(self.filterset({"q": "Other"}, self.queryset).qs.count(), 1)


class LLMUsageRecordFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for LLMUsageRecord."""

    queryset = LLMUsageRecord.objects.all()
    filterset = LLMUsageRecordFilterSet

    @classmethod
    def setUpTestData(cls):
        """One success and one failure, written the sole-writer way."""
        from nautobot_event_tracker.services.exceptions import LLMCallError  # pylint: disable=import-outside-toplevel

        cls.model = fixtures.create_llmmodel()
        cls.other_model = fixtures.create_llmmodel(name="other-model")
        fixtures.create_llmusagerecord(model=cls.model)
        fixtures.create_llmusagerecord(model=cls.other_model)
        try:
            fixtures.create_llmusagerecord(model=cls.model, client=fixtures.FakeLLMClient(error=RuntimeError("broke")))
        except LLMCallError:
            pass

    def test_success(self):
        """The success filter separates the successes from the failure."""
        self.assertEqual(self.filterset({"success": True}, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({"success": False}, self.queryset).qs.count(), 1)

    def test_model(self):
        """The model filter finds both of that model's calls."""
        self.assertEqual(self.filterset({"model": [self.model.pk]}, self.queryset).qs.count(), 2)

    def test_q_matches_the_error_text(self):
        """Finding a failure by its message is the point of recording it."""
        self.assertEqual(self.filterset({"q": "broke"}, self.queryset).qs.count(), 1)


class MCPServerFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for MCPServer."""

    queryset = MCPServer.objects.all()
    filterset = MCPServerFilterSet
    generic_filter_tests = (
        ["name"],
        ["description"],
    )

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        fixtures.create_mcpserver(name="Device Inventory", description="the read-only one")
        fixtures.create_mcpserver(name="Change System", description="switched off", enabled=False)
        fixtures.create_mcpserver(name="Spare Server", description="a third, for the generic suite")

    def test_q_matches_name_and_description(self):
        """Search covers both text fields."""
        self.assertEqual(self.filterset({"q": "Inventory"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "read-only"}, self.queryset).qs.count(), 1)

    def test_enabled(self):
        """The server-level off switch has to be findable, or nobody audits it."""
        self.assertEqual(self.filterset({"enabled": False}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"enabled": True}, self.queryset).qs.count(), 2)


class MCPToolFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for MCPTool.

    `enabled` and `mutating` are the two an operator reviews a server by, so they are the two this
    asserts on with counts that could not pass by accident.
    """

    queryset = MCPTool.objects.all()
    filterset = MCPToolFilterSet
    generic_filter_tests = (
        ["name"],
        ["server", "server__name"],
    )

    @classmethod
    def setUpTestData(cls):
        """Create test data."""
        inventory = fixtures.create_mcpserver(name="Device Inventory")
        changes = fixtures.create_mcpserver(name="Change System")
        spare = fixtures.create_mcpserver(name="Spare Server")
        fixtures.create_mcptool(server=inventory, name="get_device", mutating=False, enabled=True)
        fixtures.create_mcptool(server=changes, name="open_change", mutating=True)
        fixtures.create_mcptool(server=spare, name="spare_tool", mutating=True)

    def test_q_matches_the_tool_and_its_server(self):
        """Finding a tool by the server it is on is how a review starts."""
        self.assertEqual(self.filterset({"q": "get_device"}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"q": "Inventory"}, self.queryset).qs.count(), 1)

    def test_enabled(self):
        """The allowlist has to be readable as a list, which means filterable."""
        self.assertEqual(self.filterset({"enabled": True}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({"enabled": False}, self.queryset).qs.count(), 2)

    def test_mutating(self):
        """The review question: which of these change something."""
        self.assertEqual(self.filterset({"mutating": True}, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({"mutating": False}, self.queryset).qs.count(), 1)
        self.assertEqual(self.queryset.count(), 3)


class AgentRunFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for AgentRun. `status` is the one that matters: it is the queue of decisions."""

    queryset = AgentRun.objects.all()
    filterset = AgentRunFilterSet
    generic_filter_tests = (["status"],)

    @classmethod
    def setUpTestData(cls):
        """Runs in three states across two tickets."""
        user = fixtures.create_user(username="agent-runner")
        first = fixtures.create_ticket(user=user, title="First ticket")
        second = fixtures.create_ticket(user=user, title="Second ticket")
        fixtures.create_agentrun(ticket=first, status=AgentRunStatusChoices.WAITING_APPROVAL, started_by=user)
        fixtures.create_agentrun(ticket=first, status=AgentRunStatusChoices.COMPLETED, started_by=user)
        fixtures.create_agentrun(ticket=second, status=AgentRunStatusChoices.COMPLETED)
        # Two more states, so the generic multi-value filter test has enough to work with.
        fixtures.create_agentrun(ticket=second, status=AgentRunStatusChoices.FAILED)
        fixtures.create_agentrun(ticket=second, status=AgentRunStatusChoices.DENIED)

    def test_waiting_runs_are_findable(self):
        """ "What is waiting on somebody" is the question this page exists to answer."""
        waiting = self.filterset({"status": [AgentRunStatusChoices.WAITING_APPROVAL]}, self.queryset).qs

        self.assertEqual(waiting.count(), 1)

    def test_by_ticket(self):
        """A ticket's runs, without reading the whole table."""
        ticket = EventTicket.objects.get(title="First ticket")

        self.assertEqual(self.filterset({"ticket": [ticket.pk]}, self.queryset).qs.count(), 2)

    def test_by_who_started_it(self):
        """Not the actor, and still the thing somebody searches by."""
        self.assertEqual(self.filterset({"started_by": ["agent-runner"]}, self.queryset).qs.count(), 2)

    def test_q_matches_the_ticket_title(self):
        """A run has no name of its own, so the ticket's title is what a person types."""
        self.assertEqual(self.filterset({"q": "Second"}, self.queryset).qs.count(), 3)
        self.assertEqual(self.filterset({"q": "First"}, self.queryset).qs.count(), 2)


class AgentToolCallFilterTest(FilterTestCases.FilterTestCase):  # pylint: disable=too-many-ancestors
    """Filters for AgentToolCall. Filtering on `proposed` is an approver's work queue."""

    queryset = AgentToolCall.objects.all()
    filterset = AgentToolCallFilterSet
    generic_filter_tests = (["status"],)

    @classmethod
    def setUpTestData(cls):
        """Three calls: one waiting, one approved, one executed read-only call."""
        ticket = fixtures.create_ticket(title="Agent ticket")
        run = fixtures.create_agentrun(ticket=ticket)
        server = fixtures.create_mcpserver()
        fixtures.create_agenttoolcall(
            run=run,
            tool=fixtures.create_mcptool(server=server, name="push_config", mutating=True),
            status=AgentToolCallStatusChoices.PROPOSED,
        )
        fixtures.create_agenttoolcall(
            run=run,
            tool=fixtures.create_mcptool(server=server, name="reload_device", mutating=True),
            status=AgentToolCallStatusChoices.APPROVED,
        )
        fixtures.create_agenttoolcall(
            run=fixtures.create_agentrun(),
            tool=fixtures.create_mcptool(server=server, name="get_device", mutating=False),
            status=AgentToolCallStatusChoices.EXECUTED,
        )

    def test_proposals_are_findable(self):
        """The queue: what has been asked for and not yet decided."""
        proposed = self.filterset({"status": [AgentToolCallStatusChoices.PROPOSED]}, self.queryset).qs

        self.assertEqual(proposed.count(), 1)

    def test_by_ticket(self):
        """Through the run, because a decision is about a specific ticket."""
        ticket = EventTicket.objects.get(title="Agent ticket")

        self.assertEqual(self.filterset({"ticket": [ticket.pk]}, self.queryset).qs.count(), 2)

    def test_by_tool(self):
        """ "Has anything ever called this" is worth being able to ask of the registry."""
        tool = MCPTool.objects.get(name="push_config")

        self.assertEqual(self.filterset({"tool": [tool.pk]}, self.queryset).qs.count(), 1)

    def test_q_matches_the_tool_name(self):
        """A call has no name of its own either."""
        self.assertEqual(self.filterset({"q": "push_config"}, self.queryset).qs.count(), 1)
