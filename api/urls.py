from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from .views import (
    AssessmentViewSet,
    CaseViewSet,
    ClientViewSet,
    EnrollmentVerificationViewSet,
    ContractedServiceViewSet,
    HealthView,
    MeView,
    ProgramEligibilityListView,
    ProgramViewSet,
    ProviderViewSet,
    RegisterView,
    ScreeningViewSet,
    ZipCodeCheckView,
)
from .views_agent import (
    AgentLoginView,
    AgentRequestCodeView,
    AgentValidateView,
    AgentVerifyCodeView,
)
from .views_calltools import (
    CallToolsAgentStatusView,
    CallToolsQueuesView,
)
from .views_leads import LeadViewSet
from .views_member_app import MemberAppRequestCodeView
from .views_places import PlacesAutocompleteView, PlacesDetailsView
from .views_phones import (
    ClientPhoneDetailView,
    ClientPhonesView,
    PhoneLookupView,
)
from .views_uniteus import UniteUsCredentialCaptureView, UniteUsRunUpdateView

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
router.register("cases", CaseViewSet, basename="case")
router.register("contracted-services", ContractedServiceViewSet, basename="contracted-service")
router.register("screenings", ScreeningViewSet, basename="screening")
router.register("assessments", AssessmentViewSet, basename="assessment")
router.register(
    "enrollment-verifications",
    EnrollmentVerificationViewSet,
    basename="enrollment-verification",
)
router.register("providers", ProviderViewSet, basename="provider")
router.register("programs", ProgramViewSet, basename="program")
router.register("leads", LeadViewSet, basename="lead")

urlpatterns = [
    # Auth
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # Agent validation & login
    path("agents/validate/", AgentValidateView.as_view(), name="agent-validate"),
    path("agents/login/", AgentLoginView.as_view(), name="agent-login"),
    # Email + 2FA login: request a one-time code by company email, then verify
    path(
        "agents/request-code/",
        AgentRequestCodeView.as_view(),
        name="agent-request-code",
    ),
    path(
        "agents/verify-code/",
        AgentVerifyCodeView.as_view(),
        name="agent-verify-code",
    ),
    # Member mobile app: request a 2FA code by mobile number (SMS via Twilio
    # later; emailed to an operator inbox for now).
    path(
        "member-app/request-code/",
        MemberAppRequestCodeView.as_view(),
        name="member-app-request-code",
    ),
    path(
        "agents/<str:code>/calltools/",
        CallToolsAgentStatusView.as_view(),
        name="agent-calltools-status",
    ),
    # CallTools dialer
    path("calltools/status/", CallToolsAgentStatusView.as_view(), name="calltools-status"),
    path("calltools/queues/", CallToolsQueuesView.as_view(), name="calltools-queues"),
    # Unite Us credential capture (extension pushes the captured session here)
    path(
        "uniteus/credentials/",
        UniteUsCredentialCaptureView.as_view(),
        name="uniteus-credential-capture",
    ),
    # On-demand updater trigger (extension "Sync Now" button)
    path(
        "uniteus/run-update/",
        UniteUsRunUpdateView.as_view(),
        name="uniteus-run-update",
    ),
    # Google Places (New) proxy — doctor-address autocomplete
    path("places/autocomplete/", PlacesAutocompleteView.as_view(), name="places-autocomplete"),
    path("places/details/", PlacesDetailsView.as_view(), name="places-details"),
    # Client phones — caller-ID reverse lookup + manual assignment
    path("phones/lookup/", PhoneLookupView.as_view(), name="phone-lookup"),
    path(
        "clients/<uuid:client_id>/phones/",
        ClientPhonesView.as_view(),
        name="client-phones",
    ),
    path(
        "clients/<uuid:client_id>/phones/<uuid:client_phone_id>/",
        ClientPhoneDetailView.as_view(),
        name="client-phone-detail",
    ),
    # User
    path("me/", MeView.as_view(), name="me"),
    # Misc
    path("health/", HealthView.as_view(), name="health"),
    path("zipcodes/check/", ZipCodeCheckView.as_view(), name="zipcode-check"),
    # Program eligibilities available for a household member
    # (?member=<id>, optional ?program=&is_eligible=&model_version=)
    path(
        "program-eligibilities/",
        ProgramEligibilityListView.as_view(),
        name="program-eligibilities",
    ),
    # Customer-support web portal API (separate from the extension API above)
    path("portal/", include("api.portal.urls")),
    # Domain resources
    path("", include(router.urls)),
]
