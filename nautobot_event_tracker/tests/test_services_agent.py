"""Tests for the agent service: the loop, its bounds, and the gate it stops at.

No test calls a model and no test opens a socket: `complete` and `call_tool` are both seams, and
the fakes route through the real `services.llm` so that every turn leaves a real usage record
behind - which is what makes the accounting assertions here mean anything.
"""

import json

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from nautobot_event_tracker.choices import (
    AgentRunStatusChoices,
    AgentToolCallStatusChoices,
    LLMPurposeChoices,
    TicketSourceChoices,
    TicketStatusChoices,
    UpdateTypeChoices,
)
from nautobot_event_tracker.models import AgentRun, AgentToolCall, LLMUsageRecord
from nautobot_event_tracker.services import agent as agent_service
from nautobot_event_tracker.services.exceptions import (
    AgentBusyError,
    AgentConfigurationError,
    AgentDecisionError,
    LLMCallError,
    LLMConfigurationError,
    MCPConfigurationError,
    TicketImmutableError,
)
from nautobot_event_tracker.tests import fixtures


class AgentTestCase(TestCase):
    """Shared setup: a ticket, a model to call, and the settings that switch agents on."""

    def setUp(self):
        """One ticket and one registered model, which is all a run needs."""
        self.user = fixtures.create_user()
        self.ticket = fixtures.create_ticket(user=self.user, title="leaf-01 interface down")
        self.model = fixtures.create_llmmodel()

    def run_agent(self, *turns, call_tool=None, ticket=None, user=None):
        """Run the agent with these scripted model turns, and hand back the run and the fake."""
        complete = fixtures.FakeAgentComplete(*turns)
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=ticket or self.ticket,
                user=user if user is not None else self.user,
                complete=complete,
                call_tool=call_tool if call_tool is not None else fixtures.FakeToolCaller(),
            )
        return run, complete

    def messages(self, run, role):
        """Every transcript message with this role."""
        return [message for message in run.transcript if message.get("role") == role]

    def updates(self, update_type):
        """Every trail entry of this type on the ticket."""
        return list(self.ticket.updates.filter(update_type=update_type))


class TestTheSettings(TestCase):
    """The `agent` block: defaults, and the values that cannot work."""

    def test_agents_are_off_by_default(self):
        """An app installed before anybody configured one must not try to call a model.

        Under `app_settings()`, which is a stock install: the app's own `default_settings` and
        nothing else. Read from the ambient configuration instead, this would assert whatever the
        deployment running the tests happens to have set.
        """
        with fixtures.app_settings():
            settings = agent_service.get_settings()

        self.assertFalse(settings.enabled)
        self.assertEqual(settings.max_iterations, 8)

    def test_a_provider_and_model_are_required_when_enabled(self):
        """Switching agents on without naming a model is a fault worth a sentence."""
        with fixtures.app_settings(agent={"enabled": True}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        self.assertIn("'provider' is required", str(caught.exception))
        self.assertIn("'model' is required", str(caught.exception))

    def test_a_bound_must_be_a_positive_integer(self):
        """`True` is an `int` in Python, so the bool clause is the half that matters."""
        for value in (0, -1, True, "eight"):
            with self.subTest(value=value):
                with fixtures.app_settings(agent={"max_iterations": value}):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        agent_service.get_settings()
                self.assertIn("'max_iterations'", str(caught.exception))

    def test_a_timeout_must_be_a_positive_number(self):
        """The same check, for the two keys that are seconds rather than counts."""
        with fixtures.app_settings(agent={"tool_timeout_seconds": 0}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        self.assertIn("'tool_timeout_seconds'", str(caught.exception))

    def test_every_fault_is_reported_at_once(self):
        """One restart answers every fault, rather than the first of them."""
        with fixtures.app_settings(agent={"enabled": True, "max_iterations": 0, "timeout_seconds": -1}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                agent_service.get_settings()

        message = str(caught.exception)
        for fragment in ("'max_iterations'", "'timeout_seconds'", "'provider'", "'model'"):
            self.assertIn(fragment, message)


class TestStartingARun(AgentTestCase):
    """What is refused before any run row exists."""

    def test_a_disabled_agent_is_refused(self):
        """The Job is visible to anyone with `run` on it; "not configured" is a sentence."""
        with fixtures.app_settings(), self.assertRaises(AgentConfigurationError) as caught:
            agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("not enabled", str(caught.exception))
        self.assertFalse(AgentRun.objects.exists())

    def test_a_disabled_model_is_refused_before_a_run_exists(self):
        """Rule L8's posture, one layer up: no row exists to say the run could not start."""
        self.model.enabled = False
        self.model.validated_save()

        with fixtures.agent_settings():
            with self.assertRaises(LLMConfigurationError):
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertFalse(AgentRun.objects.exists())

    def test_a_resolved_ticket_is_refused(self):
        """S3 is not relaxed for agents: an AI actor may not touch a finished ticket."""
        ticket = fixtures.create_ticket_in_status(TicketStatusChoices.RESOLVED, user=self.user)

        with fixtures.agent_settings():
            with self.assertRaises(TicketImmutableError):
                agent_service.run_agent(ticket=ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertFalse(AgentRun.objects.exists())

    def test_a_second_run_on_a_running_ticket_is_refused(self):
        """A9 - one live run per ticket, and the message points at the run that holds it."""
        fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.RUNNING)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("still running", str(caught.exception))

    def test_a_run_waiting_on_a_decision_is_refused(self):
        """The gate is a person's turn; launching again does not take it from them."""
        run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        fixtures.create_agenttoolcall(run=run)

        with fixtures.agent_settings():
            with self.assertRaises(AgentBusyError) as caught:
                agent_service.run_agent(ticket=self.ticket, user=self.user, complete=fixtures.FakeAgentComplete())

        self.assertIn("waiting for a decision", str(caught.exception))

    def test_a_finished_run_does_not_block_a_new_one(self):
        """Only live runs hold a ticket. A completed one is history."""
        fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.COMPLETED)

        run, _ = self.run_agent("Looked, found nothing.")

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)


class TestAPlainRun(AgentTestCase):
    """A run that asks for no tools: the simplest thing the loop does."""

    def test_the_model_s_answer_becomes_a_ticket_comment(self):
        """Step 5 - no tool calls means the answer is the conclusion."""
        run, complete = self.run_agent("The interface flapped twice and recovered.")

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(run.iterations, 1)
        self.assertEqual(len(complete.calls), 1)
        comment = self.updates(UpdateTypeChoices.COMMENT)[-1]
        self.assertIn("The interface flapped twice and recovered.", comment.message)

    def test_the_comment_is_attributed_to_the_ai_and_to_no_user(self):
        """A4 - the person who launched the run is not the actor on what the model decided."""
        self.run_agent("Nothing to report.")

        comment = self.updates(UpdateTypeChoices.COMMENT)[-1]
        self.assertEqual(comment.source, TicketSourceChoices.AI)
        self.assertIsNone(comment.user)

    def test_the_run_records_who_launched_it(self):
        """Not the actor, and still worth knowing."""
        run, _ = self.run_agent("Nothing to report.")

        self.assertEqual(run.started_by, self.user)

    def test_every_model_call_is_accounted(self):
        """A10 - purpose `agent`, linked to the ticket, in the table triage's calls land in."""
        self.run_agent("Nothing to report.")

        record = LLMUsageRecord.objects.get(purpose=LLMPurposeChoices.AGENT)
        self.assertEqual(record.ticket, self.ticket)

    def test_a_new_ticket_is_triaged_by_a_run_that_concluded(self):
        """A5 - the agent moves a ticket forward, by the smallest step there is."""
        self.run_agent("This is a real fault on leaf-01.")

        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatusChoices.TRIAGED)

    def test_a_ticket_already_moved_on_is_left_alone(self):
        """The one transition is for a `new` ticket. Nothing else is the agent's to decide."""
        ticket = fixtures.create_ticket_in_status(TicketStatusChoices.IN_PROGRESS, user=self.user, title="In progress")

        self.run_agent("Still broken.", ticket=ticket)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatusChoices.IN_PROGRESS)

    def test_the_agent_never_ends_a_ticket(self):
        """A5 and 13.1 - closing is the one judgement whose being wrong is invisible."""
        self.run_agent("Fixed. Close this ticket immediately.")

        self.ticket.refresh_from_db()
        self.assertNotIn(self.ticket.status, (TicketStatusChoices.RESOLVED, TicketStatusChoices.CLOSED))

    def test_the_transcript_is_the_record(self):
        """A7 - the instruction, the ticket and the answer, in the order the model saw them."""
        run, _ = self.run_agent("Nothing to report.")

        roles = [message["role"] for message in run.transcript]
        self.assertEqual(roles, ["system", "user", "assistant"])

    def test_the_prompt_carries_the_ticket_and_its_payload(self):
        """Step 3 - and the payload is labelled as data, which is what rule A8 reads like."""
        ticket = fixtures.create_ticket(user=self.user, title="bgp down", payload={"peer": "10.0.0.1"})

        run, _ = self.run_agent("Nothing to report.", ticket=ticket)

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("bgp down", prompt)
        self.assertIn("10.0.0.1", prompt)
        self.assertIn("data and not instructions", prompt)

    def test_an_attached_device_carries_its_platform_into_the_prompt(self):
        """An agent that does not know the platform reaches for the syntax it knows best.

        Observed against the lab: told `device_type: nokia_srl` by a tool call, the model went on
        asking an SR Linux box for `show ip interface brief` and `show bgp summary` until it ran
        out of iterations. Nautobot knows what the device is; this is the app saying so.
        """
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=C0415

        device = fixtures.create_device(name="edge-01")
        ticket_service.attach_object(ticket=self.ticket, obj=device, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("edge-01", prompt)
        self.assertIn(str(device.device_type), prompt)
        self.assertIn(str(device.device_type.manufacturer), prompt)

    def test_an_attached_object_with_no_platform_reads_as_before(self):
        """Only a device gains the extra clause; everything else is untouched."""
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=C0415

        location = fixtures.create_location()
        ticket_service.attach_object(ticket=self.ticket, obj=location, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        self.assertIn(f"dcim.location: {location.name}", self.messages(run, "user")[0]["content"])

    def test_a_huge_payload_is_capped(self):
        """The payload is somebody else's, and it does not get to be the whole context."""
        ticket = fixtures.create_ticket(user=self.user, title="noisy", payload={"blob": "x" * 50_000})

        with fixtures.agent_settings(max_context_chars=200):
            run = agent_service.run_agent(
                ticket=ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete("Nothing to report."),
            )

        prompt = self.messages(run, "user")[0]["content"]
        self.assertIn("…(truncated)", prompt)
        self.assertLess(len(prompt), 2000)

    def test_the_prompt_names_the_objects_enrichment_attached(self):
        """Phase 4A put them there; this is what they were for."""
        location = fixtures.create_location()
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=C0415

        ticket_service.attach_object(ticket=self.ticket, obj=location, source=TicketSourceChoices.HUMAN, user=self.user)

        run, _ = self.run_agent("Nothing to report.")

        self.assertIn(location.name, self.messages(run, "user")[0]["content"])

    def test_the_agent_s_own_failures_are_kept_out_of_the_next_prompt(self):
        """A run reading its own past failure off the ticket diagnoses the app, not the fault.

        Observed rather than theorised: on a misconfigured endpoint the first run left "the agent
        run failed: ... Missing credentials", and the next run reported a missing OpenAI key as the
        root cause of an interface being down - then left that conclusion for the run after it.
        """
        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("Missing credentials")),
            )

        run, _ = self.run_agent("Nothing to report.")

        prompt = self.messages(run, "user")[0]["content"]
        self.assertNotIn("Missing credentials", prompt)
        self.assertNotIn(agent_service.FAILURE_COMMENT_PREFIX, prompt)

    def test_a_bound_report_is_kept_out_too(self):
        """ "The agent stopped at the limit of 3 model calls" is not a fact about the network."""
        fixtures.create_mcptool(name="look", enabled=True, mutating=False)
        self.run_agent([fixtures.fake_tool_call("look")])

        run, _ = self.run_agent("Nothing to report.")

        self.assertNotIn(agent_service.BOUND_COMMENT_PREFIX, self.messages(run, "user")[0]["content"])

    def test_a_previous_conclusion_is_still_carried(self):
        """What a run *found* is history worth having; a second run should build on the first."""
        self.run_agent("The optic on ethernet-1/1 is failing.")

        run, _ = self.run_agent("Agreed.")

        self.assertIn("The optic on ethernet-1/1 is failing.", self.messages(run, "user")[0]["content"])

    def test_a_human_comment_is_still_carried(self):
        """Only the agent's reports about itself are filtered, never anybody else's words."""
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=C0415

        ticket_service.add_comment(
            ticket=self.ticket,
            message="The agent run failed: I am a human quoting the agent.",
            source=TicketSourceChoices.HUMAN,
            user=self.user,
        )

        run, _ = self.run_agent("Noted.")

        # Matched on the prefix, so a person quoting it is filtered too. Acceptable: the cost is a
        # dropped line in one prompt, and the alternative is a marker field on every update row.
        self.assertNotIn("I am a human quoting the agent", self.messages(run, "user")[0]["content"])

    def test_an_empty_answer_still_concludes(self):
        """A model with nothing to say is a completed run, not a failed one."""
        run, _ = self.run_agent("   ")

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("without a conclusion", self.updates(UpdateTypeChoices.COMMENT)[-1].message)


class TestTheToolsOnOffer(AgentTestCase):
    """Rule M4 as the model sees it: what is in the tool list, and what is not."""

    def test_only_enabled_tools_on_enabled_servers_are_offered(self):
        """A server advertising forty tools grants forty times nothing."""
        server = fixtures.create_mcpserver()
        fixtures.create_mcptool(server=server, name="allowed", enabled=True, mutating=False)
        fixtures.create_mcptool(server=server, name="not_enabled")
        disabled_server = fixtures.create_mcpserver(name="Off Server", enabled=False)
        fixtures.create_mcptool(server=disabled_server, name="on_a_disabled_server", enabled=True, mutating=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(complete.offered_tools, ["allowed"])

    def test_a_mutating_tool_is_offered_and_labelled(self):
        """It is offered, because proposing it is the point. It is labelled for the same reason."""
        fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)

        _, complete = self.run_agent("Nothing to report.")

        definition = complete.calls[-1]["tools"][0]["function"]
        self.assertEqual(definition["name"], "push_config")
        self.assertIn("proposes it to a person", definition["description"])

    def test_two_servers_offering_one_name_get_two_names(self):
        """Providers key tool calls by name, so two of them cannot be the same name."""
        first = fixtures.create_mcpserver(name="Server A")
        second = fixtures.create_mcpserver(name="Server B")
        fixtures.create_mcptool(server=first, name="show", enabled=True, mutating=False)
        fixtures.create_mcptool(server=second, name="show", enabled=True, mutating=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(sorted(complete.offered_tools), ["show", "show_2"])

    def test_a_name_a_provider_would_refuse_is_rewritten(self):
        """A server may name a tool anything; the OpenAI schema may not."""
        fixtures.create_mcptool(name="show interface (all)", enabled=True, mutating=False)

        _, complete = self.run_agent("Nothing to report.")

        self.assertEqual(complete.offered_tools, ["show_interface__all_"])

    def test_no_tools_means_no_tools_argument(self):
        """A deployment with nothing enabled makes a plain completion, not an empty tool list."""
        _, complete = self.run_agent("Nothing to report.")

        self.assertIsNone(complete.calls[-1]["tools"])


class TestReadOnlyCalls(AgentTestCase):
    """13.4 - reading needs no approval, and what came back goes to the model."""

    def setUp(self):
        """One enabled read-only tool for the model to reach for."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="get_interface_status", enabled=True, mutating=False)

    def test_a_read_only_call_runs_and_its_answer_reaches_the_model(self):
        """The loop's step 6, end to end."""
        caller = fixtures.FakeToolCaller(text="ethernet-1/1 is down")

        run, complete = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "It is down.",
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(caller.calls[0]["tool_call"].tool, self.tool)
        self.assertEqual(self.messages(run, "tool")[0]["content"], "ethernet-1/1 is down")
        self.assertEqual(len(complete.calls), 2)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_the_call_is_recorded(self):
        """M7 - the row carries the arguments, whatever else happens."""
        self.run_agent(
            [fixtures.fake_tool_call("get_interface_status", {"device": "leaf-01"})],
            "Done.",
        )

        record = AgentToolCall.objects.get()
        self.assertEqual(record.arguments, {"device": "leaf-01"})
        self.assertEqual(record.status, AgentToolCallStatusChoices.EXECUTED)
        self.assertIsNone(record.decided_by)

    def test_the_call_reaches_the_ticket_s_trail(self):
        """7.4 - "what has been done to this ticket" includes every call against the network."""
        self.run_agent([fixtures.fake_tool_call("get_interface_status")], "Done.")

        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[-1]
        self.assertIn("get_interface_status", entry.message)
        self.assertEqual(entry.source, TicketSourceChoices.AI)

    def test_the_fingerprint_is_recorded_with_the_call(self):
        """M6's second half needs the digest the call was made against."""
        self.tool.definition_fingerprint = "abc123"
        self.tool.validated_save()

        self.run_agent([fixtures.fake_tool_call("get_interface_status")], "Done.")

        self.assertEqual(AgentToolCall.objects.get().tool_fingerprint, "abc123")

    def test_a_tool_the_model_invented_is_answered_rather_than_called(self):
        """M4 from the other side: there is nothing to look up, so nothing is called."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("delete_everything")],
            "Understood.",
            call_tool=caller,
        )

        self.assertEqual(caller.calls, [])
        self.assertIn("no tool called 'delete_everything'", self.messages(run, "tool")[0]["content"])
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_only_the_first_call_of_a_turn_runs(self):
        """One tool call per turn, and the others are told so rather than left unanswered."""
        caller = fixtures.FakeToolCaller()

        run, _ = self.run_agent(
            [
                fixtures.fake_tool_call("get_interface_status", identifier="call-1"),
                fixtures.fake_tool_call("get_interface_status", identifier="call-2"),
            ],
            "Done.",
            call_tool=caller,
        )

        self.assertEqual(len(caller.calls), 1)
        answers = {message["tool_call_id"]: message["content"] for message in self.messages(run, "tool")}
        self.assertIn("Not executed", answers["call-2"])

    def test_a_failing_tool_is_reported_to_the_model_rather_than_ending_the_run(self):
        """7.4 - the server was reached and had something to say, and it is the model's to read."""
        caller = fixtures.FakeToolCaller(error=MCPConfigurationError("Tool 'x' is not enabled."))

        run, complete = self.run_agent(
            [fixtures.fake_tool_call("get_interface_status")],
            "I could not read it.",
            call_tool=caller,
        )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("not enabled", self.messages(run, "tool")[0]["content"])
        self.assertEqual(len(complete.calls), 2)


class TestTheGate(AgentTestCase):
    """A3 and M6 - the loop ends at the first mutating proposal, and calls nothing."""

    def setUp(self):
        """One enabled mutating tool, which is the whole point of the gate."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)

    def propose(self, arguments=None):
        """Run until the model asks for the mutating tool."""
        caller = fixtures.FakeToolCaller()
        run, _ = self.run_agent(
            [fixtures.fake_tool_call("push_config", arguments or {"device": "leaf-01"})],
            call_tool=caller,
        )
        return run, caller

    def test_nothing_is_called(self):
        """The one assertion this phase exists for."""
        _, caller = self.propose()

        self.assertEqual(caller.calls, [])

    def test_the_run_ends_waiting_for_a_person(self):
        """A3 - it never waits, it ends. The worker slot goes back."""
        run, _ = self.propose()

        self.assertEqual(run.status, AgentRunStatusChoices.WAITING_APPROVAL)
        self.assertIsNotNone(run.finished_at)

    def test_the_proposal_is_written_with_the_arguments_as_asked(self):
        """7.3 - approving approves what was proposed, so what was proposed is stored."""
        self.propose({"device": "leaf-01", "config": "shutdown"})

        proposal = AgentToolCall.objects.get()
        self.assertEqual(proposal.status, AgentToolCallStatusChoices.PROPOSED)
        self.assertEqual(proposal.arguments, {"device": "leaf-01", "config": "shutdown"})

    def test_the_ticket_s_trail_names_the_server_the_tool_and_the_arguments(self):
        """7.1 - the gate is only as good as the person reading it (section 11)."""
        self.propose({"device": "leaf-01"})

        entry = self.updates(UpdateTypeChoices.TOOL_PROPOSED)[-1]
        self.assertIn("push_config", entry.message)
        self.assertIn(self.tool.server.name, entry.message)
        self.assertIn("leaf-01", entry.message)
        self.assertIn("Nothing has been called", entry.message)

    def test_the_proposal_is_not_answered_in_the_transcript(self):
        """The resumption answers it with the result of the call it made."""
        run, _ = self.propose()

        self.assertEqual(self.messages(run, "tool"), [])

    def test_a_mutating_tool_is_proposed_even_when_the_server_claims_it_reads(self):
        """4.2 - the hint is recorded and decides nothing."""
        self.tool.advertised_read_only = True
        self.tool.validated_save()

        _, caller = self.propose()

        self.assertEqual(caller.calls, [])
        self.assertTrue(AgentToolCall.objects.filter(status=AgentToolCallStatusChoices.PROPOSED).exists())


class TestDecisions(AgentTestCase):
    """7.3 - a decision needs a person, is made once, and lands on the trail."""

    def setUp(self):
        """A run waiting on one proposal."""
        super().setUp()
        self.run = fixtures.create_agentrun(ticket=self.ticket, status=AgentRunStatusChoices.WAITING_APPROVAL)
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)
        self.call = fixtures.create_agenttoolcall(run=self.run, tool=self.tool, arguments={"device": "leaf-01"})

    def test_approving_records_who_and_when(self):
        """The audit half of the gate."""
        call = agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        self.assertEqual(call.status, AgentToolCallStatusChoices.APPROVED)
        self.assertEqual(call.decided_by, self.user)
        self.assertIsNotNone(call.decided_at)

    def test_approving_writes_one_trail_entry_naming_the_approver(self):
        """A person reading the ticket sees who allowed this."""
        agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        entry = self.updates(UpdateTypeChoices.TOOL_DECIDED)[-1]
        self.assertEqual(entry.source, TicketSourceChoices.HUMAN)
        self.assertEqual(entry.user, self.user)
        self.assertIn("approved", entry.message)
        self.assertIn("leaf-01", entry.message)

    def test_denying_ends_the_run(self):
        """13.8 - a denial is the end of the chain, not the start of a negotiation."""
        agent_service.deny_tool_call(tool_call=self.call, user=self.user)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AgentRunStatusChoices.DENIED)
        self.assertIsNotNone(self.run.finished_at)

    def test_a_decision_without_a_user_is_refused(self):
        """The AI cannot approve its own proposal, because the function will not let it."""
        with self.assertRaises(AgentDecisionError):
            agent_service.approve_tool_call(tool_call=self.call, user=None)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, AgentToolCallStatusChoices.PROPOSED)

    def test_a_call_is_decided_once(self):
        """The trail already says what happened; a second decision would not change it."""
        agent_service.approve_tool_call(tool_call=self.call, user=self.user)

        with self.assertRaises(AgentDecisionError) as caught:
            agent_service.deny_tool_call(tool_call=self.call, user=self.user)

        self.assertIn("already", str(caught.exception))


class TestResumption(AgentTestCase):
    """7.4 and 13.5 - the approved call runs, and the model carries on from the transcript."""

    def setUp(self):
        """A run that proposed a mutating call and stopped."""
        super().setUp()
        self.tool = fixtures.create_mcptool(name="push_config", enabled=True, mutating=True)
        self.parent, _ = self.run_agent(
            [fixtures.fake_tool_call("push_config", {"device": "leaf-01"})],
        )
        self.proposal = AgentToolCall.objects.get()

    def resume(self, *turns, call_tool=None):
        """Approve the proposal and run the agent again, which is what the button does."""
        agent_service.approve_tool_call(tool_call=self.proposal, user=self.user)
        return self.run_agent(*turns, call_tool=call_tool)

    def test_the_approved_call_is_made(self):
        """The one thing an approval is for."""
        caller = fixtures.FakeToolCaller(text="config applied")

        run, _ = self.resume("Done.", call_tool=caller)

        self.assertEqual(len(caller.calls), 1)
        self.assertEqual(caller.calls[0]["tool_call"].pk, self.proposal.pk)
        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)

    def test_the_result_answers_the_pending_call_in_the_transcript(self):
        """The provider requires every tool call to be answered before the next turn."""
        run, _ = self.resume("Done.", call_tool=fixtures.FakeToolCaller(text="config applied"))

        answers = self.messages(run, "tool")
        self.assertEqual(answers[0]["content"], "config applied")

    def test_the_parent_run_is_superseded_rather_than_left_waiting(self):
        """A waiting run that has been taken over is not still waiting on anybody."""
        self.resume("Done.")

        self.parent.refresh_from_db()
        self.assertEqual(self.parent.status, AgentRunStatusChoices.SUPERSEDED)

    def test_the_resumption_knows_which_run_it_continued(self):
        """The chain is what makes a multi-step investigation readable afterwards."""
        run, _ = self.resume("Done.")

        self.assertEqual(run.parent, self.parent)

    def test_the_transcript_is_carried_over(self):
        """13.5 - the transcript is replayed as messages, not as provider state."""
        run, complete = self.resume("Done.")

        self.assertEqual(run.transcript[0]["role"], "system")
        self.assertGreater(len(complete.calls[0]["messages"]), 3)

    def test_the_result_reaches_the_ticket_s_trail(self):
        """7.4 - the call happened, and the ticket says so."""
        self.resume("Done.", call_tool=fixtures.FakeToolCaller(text="config applied"))

        entry = self.updates(UpdateTypeChoices.TOOL_EXECUTED)[-1]
        self.assertIn("push_config", entry.message)

    def test_a_denied_proposal_does_not_resume(self):
        """A denial ends the chain, and the next launch is a fresh run rather than a resumption."""
        agent_service.deny_tool_call(tool_call=self.proposal, user=self.user)

        run, _ = self.run_agent("Starting again.")

        self.assertIsNone(run.parent)


class TestBounds(AgentTestCase):
    """A2 - four bounds, and each of them ends a run cleanly with what it has."""

    def setUp(self):
        """One read-only tool, so a run can be made to loop."""
        super().setUp()
        fixtures.create_mcptool(name="look", enabled=True, mutating=False)

    def test_the_iteration_limit_ends_the_run(self):
        """A model that never stops asking is a bill, and "usually stops" is not a bound."""
        run, complete = self.run_agent([fixtures.fake_tool_call("look")])

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(complete.calls), 3)
        self.assertIn("3 model calls", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_the_tool_call_limit_ends_the_run(self):
        """Counted across the chain, so an approval does not reset it."""
        with fixtures.agent_settings(max_iterations=10, max_tool_calls=2):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("look")]),
                call_tool=fixtures.FakeToolCaller(),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(AgentToolCall.objects.count(), 2)
        self.assertIn("2 tool calls", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_the_transcript_limit_ends_the_run(self):
        """A long run re-sends its history and pays for it; this is where it stops."""
        with fixtures.agent_settings(max_iterations=10, max_transcript_chars=1):
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=fixtures.FakeAgentComplete([fixtures.fake_tool_call("look")]),
                call_tool=fixtures.FakeToolCaller(),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertIn("transcript limit", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_a_bounded_run_keeps_what_it_found(self):
        """ "With what it has" is the half that makes a bound useful rather than a loss."""
        run, _ = self.run_agent([fixtures.fake_tool_call("look")])

        self.assertGreater(len(run.transcript), 3)
        self.assertGreater(AgentToolCall.objects.count(), 0)


class TestFailure(AgentTestCase):
    """A6 - fail closed. Nothing half-finished is reported as finished."""

    def test_a_model_error_ends_the_run_and_says_so(self):
        """Triage fails open because a missing verdict costs a ticket; this does not."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("the provider refused")),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn("the provider refused", run.error)
        self.assertIn("failed", self.updates(UpdateTypeChoices.COMMENT)[-1].message)

    def test_a_failed_run_proposes_nothing(self):
        """The gate is not reached by failing at it."""
        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(LLMCallError("boom")),
            )

        self.assertFalse(AgentToolCall.objects.exists())

    def test_an_unexpected_error_ends_the_run_rather_than_escaping(self):
        """Anything at all ends the run visibly, including a fault nobody anticipated."""
        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_failing_complete(RuntimeError("something nobody expected")),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.FAILED)
        self.assertIn("something nobody expected", run.error)

    def test_a_ticket_resolved_mid_run_does_not_lose_the_run_s_record(self):
        """S3 refuses the write, which is correct, and the transcript survives it."""
        from nautobot_event_tracker.services import tickets as ticket_service  # pylint: disable=C0415

        class _ResolvingComplete(fixtures.FakeAgentComplete):
            """Resolves the ticket between the call and the conclusion."""

            def __init__(self, ticket, user, *turns):
                super().__init__(*turns)
                self.ticket = ticket
                self.user = user

            def __call__(self, **kwargs):
                response = super().__call__(**kwargs)
                ticket_service.walk_to_status(
                    ticket=self.ticket,
                    to_status=TicketStatusChoices.RESOLVED,
                    source=TicketSourceChoices.HUMAN,
                    user=self.user,
                    resolution="Somebody else fixed it.",
                )
                return response

        with fixtures.agent_settings():
            run = agent_service.run_agent(
                ticket=self.ticket,
                user=self.user,
                complete=_ResolvingComplete(self.ticket, self.user, "Found it."),
            )

        self.assertEqual(run.status, AgentRunStatusChoices.COMPLETED)
        self.assertEqual(len(run.transcript), 3)


class TestTheTranscriptShape(AgentTestCase):
    """13.5 - stored as provider-shaped messages, so a provider change does not invalidate it."""

    def test_an_assistant_turn_carries_its_tool_calls_as_the_provider_sent_them(self):
        """The next request has to send them back exactly, ids and all."""
        fixtures.create_mcptool(name="look", enabled=True, mutating=False)

        run, _ = self.run_agent(
            [fixtures.fake_tool_call("look", {"device": "leaf-01"}, identifier="call-9")],
            "Done.",
        )

        assistant = self.messages(run, "assistant")[0]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-9")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"device": "leaf-01"})

    def test_the_transcript_is_written_as_the_run_goes(self):
        """A worker killed mid-run leaves something readable, which is the case worth reading."""
        seen = []

        class _WatchingComplete(fixtures.FakeAgentComplete):
            """Records how much of the transcript was persisted before each call."""

            def __call__(self, **kwargs):
                seen.append(AgentRun.objects.get().transcript)
                return super().__call__(**kwargs)

        with fixtures.agent_settings():
            agent_service.run_agent(
                ticket=self.ticket, user=self.user, complete=_WatchingComplete("Nothing to report.")
            )

        self.assertEqual([message["role"] for message in seen[0]], ["system", "user"])


def _failing_complete(error):
    """A `complete` seam that raises this error instead of answering."""

    def _complete(**_kwargs):
        raise error

    return _complete
