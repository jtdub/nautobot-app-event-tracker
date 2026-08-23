"""Django API urlpatterns declaration for nautobot_event_tracker app."""

from nautobot.apps.api import OrderedDefaultRouter

from nautobot_event_tracker.api import views

router = OrderedDefaultRouter()
router.register("event-types", views.EventTypeViewSet)
router.register("tickets", views.EventTicketViewSet)
router.register("ticket-updates", views.TicketUpdateViewSet)
router.register("ingestion-stats", views.IngestionStatsViewSet)
router.register("llm-providers", views.LLMProviderViewSet)
router.register("llm-models", views.LLMModelViewSet)
router.register("llm-usage", views.LLMUsageRecordViewSet)
router.register("mcp-servers", views.MCPServerViewSet)
router.register("mcp-tools", views.MCPToolViewSet)
router.register("agent-runs", views.AgentRunViewSet)
router.register("agent-tool-calls", views.AgentToolCallViewSet)

app_name = "nautobot_event_tracker-api"
urlpatterns = router.urls
