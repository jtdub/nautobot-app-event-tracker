# 0008 — UI Component Framework only

**Status:** Accepted
**Phase:** 1

## Context

Nautobot 3.x provides `NautobotUIViewSet` together with a UI Component Framework: `ObjectDetailContent` holding declarative panels (`ObjectFieldsPanel`, `ObjectsTablePanel`, `GroupedKeyValueTablePanel`, `EChartsPanel`) and buttons (`Button`, `PostButton`, `DropdownButton`), each with a `should_render()` hook and `required_permissions`.

The alternative is what app authors did before it existed: write Django templates extending `generic/object_retrieve.html` and fill in blocks by hand. Hand-written templates are how an app's UI drifts away from core's. Core changes its base templates between minor versions; an app that reaches into them inherits every one of those changes as a breakage, and the app ends up looking subtly unlike the rest of Nautobot in the meantime.

There is also a correctness reason specific to this app. Transition buttons must appear only for states the workflow graph actually permits from the ticket's current status. In a template that becomes a chain of `{% if %}` conditions duplicating the graph in the template language — a second copy of the rules, in the one place with no test coverage. ADR 0002 exists to prevent exactly that duplication.

## Decision

The UI is built from `NautobotUIViewSet` and the UI Component Framework. The app ships no hand-written page templates.

For Phase 1 specifically:

- The ticket detail view is an `ObjectDetailContent` of declarative panels.
- Attached network objects render in a `GroupedKeyValueTablePanel`, grouped by object type, so devices, interfaces, and circuits appear under their own headings.
- The update trail renders as a chronological table panel.
- Each legal transition is a `PostButton` whose `should_render()` returns `True` only when the target state is in the workflow graph's edge set for the ticket's current status and the user holds `transition_eventticket`. The graph is consulted; it is never restated.
- The button posts to the same service-layer-backed endpoint the REST API uses. There is no second implementation of a transition.

Later phases add analytics with `EChartsPanel`, keeping charts inside the same framework.

## Amendment (Phase 5B): one template, for the page that has no object

The framework renders panels *inside an object detail page*. `Tab.should_render_content()` asks the object for its own URL before deciding whether to draw, `ObjectDetailContent` injects the standard extras tabs around one, and core ships no template that takes a bare list of panels. Every object-less page in Nautobot core — the profile, the token list, the plugin list — renders a template of its own.

The [analytics dashboard](../specs/phase-5b-analytics.md) is a page about no object. It could have been avoided by hanging the charts off an existing detail page or off Nautobot's home page, and both would have been contrivances: the page has a window control and a route, and pretending otherwise to satisfy a rule is how a rule stops meaning anything.

So the app ships exactly one template, `templates/nautobot_event_tracker/dashboard.html`. It extends `base.html` and nothing else, and its body is the window form plus `{% include "components/layout/two_over_one.html" %}` — core's own panel grid, included rather than copied. It draws no panel, no table, no grid and no button of its own.

What this decision is actually about is preserved: the hazard is coupling to core's page internals and drifting into hand-written UI, not the existence of a file. Extending `generic/object_retrieve.html` inherits every change core makes to it; extending `base.html` and delegating to the framework inherits the framework.

`tests/test_guards.py::TemplateGuardTest` changes from "no `.html` anywhere" to an explicit one-file allowlist over everything under `templates/`, and asserts two things about the allowed file: that it extends `base.html` alone, and that it emits no grid or table markup of its own. The second check is the one that matters, and it was added because the first draft passed the first check while carrying a private copy of core's layout template — which is precisely the drift, inside the exception. Adding a second entry to the allowlist is a decision about this ADR rather than a test fix.

## Consequences

**Good.** The app tracks core's look and behaviour through upgrades without edits. Permission checks and conditional rendering are declared where they can be unit tested. Transition legality has exactly one implementation.

**Bad.** One page is now a template, and the guard that used to be a flat prohibition is a list. A list is easier to add to than a prohibition is to overturn, which is why the list is asserted and the ADR says what adding to it means.

**Bad.** The framework bounds what the UI can look like. A layout it does not support is not available, and the correct response is to accept a plainer page rather than reach for a template — a discipline that will occasionally be frustrating.

**Bad.** The framework is young. Its API may shift within the 3.x line, and this app is exposed to that in a way a template-based app would not be. The narrow Nautobot version pin (`>=3.2,<4.0`) is partly for this reason.

## Alternatives considered

**Hand-written templates extending core.** Rejected: coupled to core template internals, duplicates the workflow graph in an untestable place.

**A separate front-end application against the REST API.** Rejected: it abandons Nautobot's navigation, permissions, and object-picker integration, and would be a second deployment unit for a ticketing UI.
