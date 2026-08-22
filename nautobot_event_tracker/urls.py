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
    path("docs/", RedirectView.as_view(url=static("nautobot_event_tracker/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
