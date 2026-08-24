"""Django urlpatterns declaration for nautobot_event_tracker app."""

from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView
from nautobot.apps.urls import NautobotUIViewSetRouter

from nautobot_event_tracker import views

app_name = "nautobot_event_tracker"
router = NautobotUIViewSetRouter()

router.register("event-types", views.EventTypeUIViewSet)
router.register("tickets", views.EventTicketUIViewSet)
router.register("ingestion-stats", views.IngestionStatsUIViewSet)
router.register("llm-providers", views.LLMProviderUIViewSet)
router.register("llm-models", views.LLMModelUIViewSet)
router.register("llm-usage", views.LLMUsageRecordUIViewSet)
router.register("mcp-servers", views.MCPServerUIViewSet)
router.register("mcp-tools", views.MCPToolUIViewSet)
router.register("agent-runs", views.AgentRunUIViewSet)
router.register("agent-tool-calls", views.AgentToolCallUIViewSet)
router.register("ticket-embeddings", views.TicketEmbeddingUIViewSet)

urlpatterns = [
    path(
        "tickets/<uuid:pk>/transition/",
        views.EventTicketTransitionView.as_view(),
        name="eventticket_transition",
    ),
    path(
        "tickets/<uuid:pk>/attach/",
        views.EventTicketAttachView.as_view(),
        name="eventticket_attach",
    ),
    path(
        "tickets/<uuid:pk>/detach/",
        views.EventTicketDetachView.as_view(),
        name="eventticket_detach",
    ),
    path(
        "mcp-servers/<uuid:pk>/discover/",
        views.MCPServerDiscoverView.as_view(),
        name="mcpserver_discover",
    ),
    # Two routes rather than one taking a parameter: which decision is being made is the thing
    # this whole phase exists to control, and it should not be a form field.
    path(
        "agent-tool-calls/<uuid:pk>/approve/",
        views.AgentToolCallApproveView.as_view(),
        name="agenttoolcall_approve",
    ),
    path(
        "agent-tool-calls/<uuid:pk>/deny/",
        views.AgentToolCallDenyView.as_view(),
        name="agenttoolcall_deny",
    ),
    path("docs/", RedirectView.as_view(url=static("nautobot_event_tracker/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
