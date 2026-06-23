"""URL routing for the `/api/portal/` support-portal API."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .auth import PortalRequestCodeView, PortalVerifyCodeView
from .views_dashboard import DashboardView
from .views_leads import (
    PortalLeadDetailView,
    PortalLeadNotesView,
    PortalLeadsView,
    PortalProgramCategoriesView,
    PortalScreenersView,
)
from .views_members import (
    HouseholdMemberEditView,
    MemberCasesView,
    MemberDetailView,
    MemberHistoryDetailView,
    MemberHistoryView,
    MemberHouseholdView,
    MemberInsuranceView,
    MemberNotesView,
    MemberOrdersView,
    MemberSocialCoverageView,
    MemberTicketsView,
    MemberVerificationCreateView,
    MembersListView,
    MembersStatsView,
)
from .views_places import (
    PortalPlacesAutocompleteView,
    PortalPlacesDetailsView,
)
from .views_orders import (
    DeliveryCompaniesListView,
    KitchensListView,
    PurchaseOrderDeliveryOrdersView,
    PurchaseOrdersStatsView,
    PurchaseOrdersView,
    SendToDeliveryView,
    SendToKitchenView,
)
from .views_settings import (
    DeliveryCompanyIntegrationDetailView,
    DeliveryCompanyIntegrationSetPrimaryView,
    DeliveryCompanyViewSet,
    DietaryTagViewSet,
    KitchenIntegrationDetailView,
    KitchenViewSet,
    MenuTypeViewSet,
)
from .views_tickets import (
    AgentsListView,
    TicketDetailView,
    TicketNotesView,
    TicketsStatsView,
    TicketTypesListView,
    WorkQueueView,
)

router = SimpleRouter()
router.register("settings/menu-types", MenuTypeViewSet, basename="portal-menu-type")
router.register("settings/dietary-tags", DietaryTagViewSet, basename="portal-dietary-tag")
router.register("settings/kitchens", KitchenViewSet, basename="portal-kitchen")
router.register(
    "settings/delivery-companies", DeliveryCompanyViewSet, basename="portal-delivery-company"
)

urlpatterns = [
    # Auth
    path("auth/request-code/", PortalRequestCodeView.as_view(), name="portal-request-code"),
    path("auth/verify-code/", PortalVerifyCodeView.as_view(), name="portal-verify-code"),

    # Members + sub-resources
    path("members/", MembersListView.as_view(), name="portal-members"),
    path("members/stats/", MembersStatsView.as_view(), name="portal-members-stats"),
    path("members/<uuid:client_id>/", MemberDetailView.as_view(), name="portal-member-detail"),
    path("members/<uuid:client_id>/insurance/", MemberInsuranceView.as_view()),
    path("members/<uuid:client_id>/social-coverage/", MemberSocialCoverageView.as_view()),
    path("members/<uuid:client_id>/history/", MemberHistoryView.as_view()),
    path("members/<uuid:client_id>/history/<int:event_id>/", MemberHistoryDetailView.as_view()),
    path("members/<uuid:client_id>/orders/", MemberOrdersView.as_view()),
    path("members/<uuid:client_id>/household/", MemberHouseholdView.as_view()),
    path(
        "members/<uuid:client_id>/household/members/<int:member_id>/",
        HouseholdMemberEditView.as_view(),
    ),
    path("members/<uuid:client_id>/notes/", MemberNotesView.as_view()),
    path("members/<uuid:client_id>/cases/", MemberCasesView.as_view()),
    path("members/<uuid:client_id>/tickets/", MemberTicketsView.as_view()),
    path("members/<uuid:client_id>/verification/", MemberVerificationCreateView.as_view()),

    # Work queue (global tickets)
    path("tickets/", WorkQueueView.as_view(), name="portal-tickets"),
    path("tickets/types/", TicketTypesListView.as_view(), name="portal-ticket-types"),
    path("tickets/stats/", TicketsStatsView.as_view(), name="portal-tickets-stats"),
    path("tickets/<int:ticket_id>/", TicketDetailView.as_view(), name="portal-ticket-detail"),
    path("tickets/<int:ticket_id>/notes/", TicketNotesView.as_view()),
    path("agents/", AgentsListView.as_view(), name="portal-agents"),

    # Leads (agent-created from the Work Queue + Leads page)
    path("leads/", PortalLeadsView.as_view(), name="portal-leads"),
    path("leads/<uuid:lead_id>/", PortalLeadDetailView.as_view(), name="portal-lead-detail"),
    path("leads/<uuid:lead_id>/notes/", PortalLeadNotesView.as_view(), name="portal-lead-notes"),
    path("screeners/", PortalScreenersView.as_view(), name="portal-screeners"),
    path("program-categories/", PortalProgramCategoriesView.as_view(), name="portal-program-categories"),

    # Orders (global purchase orders)
    path("purchase-orders/", PurchaseOrdersView.as_view(), name="portal-purchase-orders"),
    path("purchase-orders/stats/", PurchaseOrdersStatsView.as_view()),
    path("purchase-orders/<uuid:po_id>/delivery-orders/", PurchaseOrderDeliveryOrdersView.as_view()),
    path("purchase-orders/<uuid:po_id>/send-to-kitchen/", SendToKitchenView.as_view()),
    path("purchase-orders/<uuid:po_id>/send-to-delivery/", SendToDeliveryView.as_view()),
    path("kitchens/", KitchensListView.as_view(), name="portal-kitchens"),
    path("delivery-companies/", DeliveryCompaniesListView.as_view(), name="portal-delivery-companies"),

    # Settings integration sub-resources (not covered by the viewset router)
    path(
        "settings/kitchen-integrations/<uuid:integration_id>/",
        KitchenIntegrationDetailView.as_view(),
    ),
    path(
        "settings/delivery-company-integrations/<uuid:integration_id>/",
        DeliveryCompanyIntegrationDetailView.as_view(),
    ),
    path(
        "settings/delivery-company-integrations/<uuid:integration_id>/set-primary/",
        DeliveryCompanyIntegrationSetPrimaryView.as_view(),
    ),

    # Google Places proxy — delivery-address autocomplete (mirrors the
    # extension's doctor-address autocomplete; key stays server-side).
    path("places/autocomplete/", PortalPlacesAutocompleteView.as_view(), name="portal-places-autocomplete"),
    path("places/details/", PortalPlacesDetailsView.as_view(), name="portal-places-details"),

    # Dashboard
    path("dashboard/", DashboardView.as_view(), name="portal-dashboard"),

    # Settings CRUD viewsets
    path("", include(router.urls)),
]
