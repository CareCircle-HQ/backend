"""URL routing for the `/api/portal/` support-portal API."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .auth import PortalRequestCodeView, PortalVerifyCodeView
from .views_care_management import CareManagementListView
from .views_cs_dashboard import (
    CSDashboardSummaryView,
    CSDashboardTrendsView,
    CSTicketManagerStatsView,
)
from .views_delivery_address import DeliveryAddressListView
from .views_member_uniteus import MemberUniteUsRefreshView
from .views_dashboard import DashboardServingListView, DashboardView
from .views_dashboard_verification import (
    VerificationDashboardListView,
    VerificationDashboardView,
)
from .views_dashboard_logistics import (
    DistributionKitchenMembersView,
    DistributionOverviewView,
    LogisticsDashboardListView,
    LogisticsDashboardView,
)
from .views_leads import (
    PortalLeadDetailView,
    PortalLeadNotesView,
    PortalLeadsView,
    PortalProgramCategoriesView,
    PortalScreenersView,
)
from .views_members import (
    BulkAssignBoxesView,
    BulkAssignMealsView,
    FoodAllergiesListView,
    HouseholdMemberEditView,
    LeadSourcesListView,
    MemberAssignKitchenView,
    MemberCadenceView,
    MemberCaseAuditView,
    MemberCaseDetailView,
    MemberCaseHistoryView,
    MemberCasesView,
    MemberDetailView,
    MemberDiagnosticView,
    MemberHistoryDetailView,
    MemberHistoryView,
    MemberHouseholdAddView,
    MemberHouseholdSearchView,
    MemberHouseholdView,
    MemberProductTypeView,
    MemberWarningsView,
    MemberInternalCaseDescriptionsView,
    MemberInsuranceView,
    MemberKitchenOptionsView,
    MemberKitchenView,
    MemberNotesView,
    MemberOrdersView,
    MemberPhoneDetailView,
    MemberPhonesView,
    MemberServiceCancelView,
    MemberServiceHoldView,
    MemberServiceReactivateView,
    MemberServiceResumeView,
    MemberDismissAttentionView,
    MemberDoctorView,
    MemberRequestVerificationView,
    MemberSocialCoverageView,
    MemberTicketsView,
    MemberVerificationCreateView,
    MemberVerificationDisregardView,
    MembersListView,
    MembersStatsView,
    NeedAttestationMembersListView,
    NoNavigationMembersListView,
    UnlinkedMembersListView,
    MenuTypesListView,
    TeamsListView,
)
from .views_places import (
    PortalPlacesAutocompleteView,
    PortalPlacesDetailsView,
)
from .views_orders import (
    CancelPurchaseOrderView,
    DeliveryCompaniesListView,
    KitchenExportView,
    KitchensListView,
    PurchaseOrderDeliveryOrdersView,
    PurchaseOrderGenerateView,
    PurchaseOrderPreviewLateView,
    PurchaseOrderPreviewRefreshView,
    PurchaseOrderPreviewView,
    PurchaseOrderReportDataView,
    PurchaseOrderReportView,
    PurchaseOrderSplitView,
    PurchaseOrdersStatsView,
    PurchaseOrdersView,
    SendToDeliveryView,
    SendToKitchenView,
)
from .views_delivery_calendar import MemberDeliveryCalendarView
from .views_kitchen_output import KitchenOutputView
from .views_po_blockers import POBlockersFixView, POBlockersStatsView, POBlockersView
from .views_reports import (
    AllMembersReportView,
    CasesReportView,
    MembersByLeadSourceReportView,
    MembersPendingVerificationReportView,
)
from .views_activity import ActivityFiltersView, ActivityLogView
from .views_service_area import (
    AllowedStateDetailView,
    AllowedStatesView,
    ExcludedZipCodeDetailView,
    ExcludedZipCodesView,
)
from .views_imports import (
    ImportActivityView,
    ImportPresignView,
    ImportRunDetailView,
    ImportRunsView,
    ImportStartView,
    ImportUploadView,
    UniteUsAgentDetailView,
    UniteUsAgentsView,
    UniteUsExportDetailView,
    UniteUsExportPollView,
    UniteUsExportsView,
)
from .views_settings import (
    CadenceViewSet,
    CrmAgentViewSet,
    DeliveryCompanyIntegrationDetailView,
    DeliveryCompanyIntegrationSetPrimaryView,
    DeliveryCompanyViewSet,
    DietaryTagViewSet,
    KitchenIntegrationDetailView,
    KitchenViewSet,
    MenuTypeViewSet,
    ProgramMainCategoryViewSet,
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
router.register("settings/cadences", CadenceViewSet, basename="portal-cadence")
router.register("settings/kitchens", KitchenViewSet, basename="portal-kitchen")
router.register(
    "settings/delivery-companies", DeliveryCompanyViewSet, basename="portal-delivery-company"
)
router.register("settings/agents", CrmAgentViewSet, basename="portal-crm-agent")
router.register(
    "settings/program-main-categories",
    ProgramMainCategoryViewSet,
    basename="portal-program-main-category",
)

urlpatterns = [
    # Auth
    path("auth/request-code/", PortalRequestCodeView.as_view(), name="portal-request-code"),
    path("auth/verify-code/", PortalVerifyCodeView.as_view(), name="portal-verify-code"),

    # Members + sub-resources
    path("members/", MembersListView.as_view(), name="portal-members"),
    path("members/stats/", MembersStatsView.as_view(), name="portal-members-stats"),
    path("members/unlinked/", UnlinkedMembersListView.as_view(), name="portal-members-unlinked"),
    path("members/no-navigation/", NoNavigationMembersListView.as_view(), name="portal-members-no-navigation"),
    path("members/need-attestation/", NeedAttestationMembersListView.as_view(), name="portal-members-need-attestation"),
    path("menu-types/", MenuTypesListView.as_view(), name="portal-menu-types"),
    path("food-allergies/", FoodAllergiesListView.as_view(), name="portal-food-allergies"),
    path("lead-sources/", LeadSourcesListView.as_view(), name="portal-lead-sources"),
    path("teams/", TeamsListView.as_view(), name="portal-teams"),
    path("members/<uuid:client_id>/", MemberDetailView.as_view(), name="portal-member-detail"),
    path("members/<uuid:client_id>/insurance/", MemberInsuranceView.as_view()),
    path("members/<uuid:client_id>/social-coverage/", MemberSocialCoverageView.as_view()),
    path("members/<uuid:client_id>/doctor/", MemberDoctorView.as_view(), name="portal-member-doctor"),
    path("members/<uuid:client_id>/phones/", MemberPhonesView.as_view()),
    path(
        "members/<uuid:client_id>/phones/<uuid:client_phone_id>/",
        MemberPhoneDetailView.as_view(),
    ),
    path("members/<uuid:client_id>/history/", MemberHistoryView.as_view()),
    path("members/<uuid:client_id>/history/<int:event_id>/", MemberHistoryDetailView.as_view()),
    path("members/<uuid:client_id>/orders/", MemberOrdersView.as_view()),
    path(
        "members/<uuid:client_id>/delivery-calendar/",
        MemberDeliveryCalendarView.as_view(),
    ),
    path("members/<uuid:client_id>/household/", MemberHouseholdView.as_view()),
    path("members/<uuid:client_id>/household/search/", MemberHouseholdSearchView.as_view()),
    path("members/<uuid:client_id>/household/add/", MemberHouseholdAddView.as_view()),
    path(
        "members/<uuid:client_id>/product-type/",
        MemberProductTypeView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/warnings/",
        MemberWarningsView.as_view(),
    ),
    # TEMPORARY: internal-service case descriptions on the Household tab.
    path(
        "members/<uuid:client_id>/internal-case-descriptions/",
        MemberInternalCaseDescriptionsView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/household/members/<int:member_id>/",
        HouseholdMemberEditView.as_view(),
    ),
    # Logistics / kitchen assignment
    path("members/bulk-assign-boxes/", BulkAssignBoxesView.as_view()),
    path("members/bulk-assign-meals/", BulkAssignMealsView.as_view()),
    path("members/<uuid:client_id>/kitchen-options/", MemberKitchenOptionsView.as_view()),
    path("members/<uuid:client_id>/assign-kitchen/", MemberAssignKitchenView.as_view()),
    path("members/<uuid:client_id>/kitchen/", MemberKitchenView.as_view()),
    path("members/<uuid:client_id>/cadence/", MemberCadenceView.as_view()),
    path("members/<uuid:client_id>/hold/", MemberServiceHoldView.as_view()),
    path("members/<uuid:client_id>/resume/", MemberServiceResumeView.as_view()),
    path("members/<uuid:client_id>/cancel/", MemberServiceCancelView.as_view()),
    path("members/<uuid:client_id>/reactivate/", MemberServiceReactivateView.as_view()),
    path("members/<uuid:client_id>/notes/", MemberNotesView.as_view()),
    path("members/<uuid:client_id>/cases/", MemberCasesView.as_view()),
    path(
        "members/<uuid:client_id>/cases/<uuid:case_id>/",
        MemberCaseDetailView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/refresh-uniteus/",
        MemberUniteUsRefreshView.as_view(),
        name="portal-member-refresh-uniteus",
    ),
    path(
        "members/<uuid:client_id>/cases/<uuid:case_id>/history/",
        MemberCaseHistoryView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/cases/<uuid:case_id>/audit/",
        MemberCaseAuditView.as_view(),
    ),
    path("members/<uuid:client_id>/tickets/", MemberTicketsView.as_view()),
    path("members/<uuid:client_id>/verification/", MemberVerificationCreateView.as_view()),
    path(
        "members/<uuid:client_id>/verification/disregard/",
        MemberVerificationDisregardView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/dismiss-attention/",
        MemberDismissAttentionView.as_view(),
    ),
    path(
        "members/<uuid:client_id>/request-verification/",
        MemberRequestVerificationView.as_view(),
    ),
    path("members/<uuid:client_id>/diagnostic/", MemberDiagnosticView.as_view()),

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
    path("purchase-orders/preview/", PurchaseOrderPreviewView.as_view()),
    path("purchase-orders/preview/refresh/", PurchaseOrderPreviewRefreshView.as_view()),
    path("purchase-orders/preview/late/", PurchaseOrderPreviewLateView.as_view()),
    path("purchase-orders/generate/", PurchaseOrderGenerateView.as_view()),
    path("purchase-orders/<uuid:po_id>/split/", PurchaseOrderSplitView.as_view()),
    path("purchase-orders/<uuid:po_id>/delivery-orders/", PurchaseOrderDeliveryOrdersView.as_view()),
    path("purchase-orders/<uuid:po_id>/cancel/", CancelPurchaseOrderView.as_view()),
    path("purchase-orders/<uuid:po_id>/send-to-kitchen/", SendToKitchenView.as_view()),
    path("purchase-orders/<uuid:po_id>/kitchen-export/", KitchenExportView.as_view()),
    path("purchase-orders/<uuid:po_id>/report/", PurchaseOrderReportView.as_view()),
    path("purchase-orders/<uuid:po_id>/report-data/", PurchaseOrderReportDataView.as_view()),
    path("purchase-orders/<uuid:po_id>/send-to-delivery/", SendToDeliveryView.as_view()),
    path("kitchens/", KitchensListView.as_view(), name="portal-kitchens"),
    path("delivery-companies/", DeliveryCompaniesListView.as_view(), name="portal-delivery-companies"),

    # Kitchen Output: one member's verification inputs vs resolved kitchen output
    path(
        "kitchen-output/<uuid:client_id>/",
        KitchenOutputView.as_view(),
        name="portal-kitchen-output",
    ),

    # PO Blockers: members with a live plan that won't reach a Purchase Order
    path("po-blockers/", POBlockersView.as_view(), name="portal-po-blockers"),
    path("po-blockers/stats/", POBlockersStatsView.as_view(), name="portal-po-blockers-stats"),
    path("po-blockers/fix/", POBlockersFixView.as_view(), name="portal-po-blockers-fix"),

    # Settings > Import: manual Unite Us CSV upload + run history
    path("settings/imports/", ImportRunsView.as_view(), name="portal-import-runs"),
    path("settings/imports/upload/", ImportUploadView.as_view(), name="portal-import-upload"),
    # Async S3 flow: presign -> browser PUTs to S3 -> start (enqueue) -> poll detail
    path("settings/imports/presign/", ImportPresignView.as_view(), name="portal-import-presign"),
    path("settings/imports/<int:run_id>/", ImportRunDetailView.as_view(), name="portal-import-detail"),
    path("settings/imports/<int:run_id>/start/", ImportStartView.as_view(), name="portal-import-start"),
    # Settings > Import Activity: rollup of follow-up actions across case imports
    path("settings/import-activity/", ImportActivityView.as_view(), name="portal-import-activity"),
    # Settings > Import: automated Unite Us exports (request -> poll -> import)
    path("settings/uniteus-exports/", UniteUsExportsView.as_view(), name="portal-uniteus-exports"),
    path("settings/uniteus-exports/poll/", UniteUsExportPollView.as_view(), name="portal-uniteus-exports-poll"),
    path("settings/uniteus-exports/<int:export_pk>/", UniteUsExportDetailView.as_view(), name="portal-uniteus-export-detail"),
    # Settings > Activity Log: cross-client timeline feed (admin audit view)
    path("activity/", ActivityLogView.as_view(), name="portal-activity"),
    path("activity/filters/", ActivityFiltersView.as_view(), name="portal-activity-filters"),
    # Settings > Import: Unite Us agents allowlist (gates which cases import)
    path("settings/unite-us-agents/", UniteUsAgentsView.as_view(), name="portal-unite-us-agents"),
    path(
        "settings/unite-us-agents/<uuid:agent_id>/",
        UniteUsAgentDetailView.as_view(),
        name="portal-unite-us-agent-detail",
    ),
    # Settings > Excluded ZIP Codes: delivery-coverage exclusion list
    path(
        "settings/excluded-zip-codes/",
        ExcludedZipCodesView.as_view(),
        name="portal-excluded-zip-codes",
    ),
    path(
        "settings/excluded-zip-codes/<int:zip_id>/",
        ExcludedZipCodeDetailView.as_view(),
        name="portal-excluded-zip-code-detail",
    ),
    # Settings > Allowed States: served-states allow-list
    path(
        "settings/allowed-states/",
        AllowedStatesView.as_view(),
        name="portal-allowed-states",
    ),
    path(
        "settings/allowed-states/<str:code>/",
        AllowedStateDetailView.as_view(),
        name="portal-allowed-state-detail",
    ),

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

    # Reports (Admin > Reports): management-only CSV exports
    path(
        "reports/members-by-lead-source/",
        MembersByLeadSourceReportView.as_view(),
        name="portal-report-members-by-lead-source",
    ),
    path(
        "reports/members-pending-verification/",
        MembersPendingVerificationReportView.as_view(),
        name="portal-report-members-pending-verification",
    ),
    path(
        "reports/all-members/",
        AllMembersReportView.as_view(),
        name="portal-report-all-members",
    ),
    path(
        "reports/cases/",
        CasesReportView.as_view(),
        name="portal-report-cases",
    ),

    # Care Management (Customer Service)
    path(
        "care-management/",
        CareManagementListView.as_view(),
        name="portal-care-management",
    ),

    # Delivery Address (Customer Service)
    path(
        "delivery-addresses/",
        DeliveryAddressListView.as_view(),
        name="portal-delivery-addresses",
    ),

    # CS Dashboard (Customer Service command center)
    path(
        "cs-dashboard/",
        CSDashboardSummaryView.as_view(),
        name="portal-cs-dashboard",
    ),
    path(
        "cs-dashboard/trends/",
        CSDashboardTrendsView.as_view(),
        name="portal-cs-dashboard-trends",
    ),
    path(
        "cs-dashboard/ticket-stats/",
        CSTicketManagerStatsView.as_view(),
        name="portal-cs-dashboard-ticket-stats",
    ),

    # Dashboard
    path("dashboard/", DashboardView.as_view(), name="portal-dashboard"),
    path(
        "dashboard/serving/<str:reason>/",
        DashboardServingListView.as_view(),
        name="portal-dashboard-serving-list",
    ),
    path(
        "dashboard/verification/",
        VerificationDashboardView.as_view(),
        name="portal-dashboard-verification",
    ),
    path(
        "dashboard/verification/<str:reason>/",
        VerificationDashboardListView.as_view(),
        name="portal-dashboard-verification-list",
    ),
    path(
        "dashboard/logistics/",
        LogisticsDashboardView.as_view(),
        name="portal-dashboard-logistics",
    ),
    path(
        "dashboard/logistics/<str:reason>/",
        LogisticsDashboardListView.as_view(),
        name="portal-dashboard-logistics-list",
    ),
    path(
        "dashboard/distribution/",
        DistributionOverviewView.as_view(),
        name="portal-dashboard-distribution",
    ),
    path(
        "dashboard/distribution/<str:kitchen>/members/",
        DistributionKitchenMembersView.as_view(),
        name="portal-dashboard-distribution-members",
    ),

    # Settings CRUD viewsets
    path("", include(router.urls)),
]
