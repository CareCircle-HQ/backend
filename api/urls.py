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
    ContractedServiceViewSet,
    HealthView,
    MeView,
    ProgramViewSet,
    ProviderViewSet,
    RegisterView,
    ScreeningViewSet,
)
from .views_agent import AgentLoginView, AgentValidateView
from .views_calltools import CallToolsAgentStatusView, CallToolsCampaignsView

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
router.register("cases", CaseViewSet, basename="case")
router.register("contracted-services", ContractedServiceViewSet, basename="contracted-service")
router.register("screenings", ScreeningViewSet, basename="screening")
router.register("assessments", AssessmentViewSet, basename="assessment")
router.register("providers", ProviderViewSet, basename="provider")
router.register("programs", ProgramViewSet, basename="program")

urlpatterns = [
    # Auth
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # Agent validation & login
    path("agents/validate/", AgentValidateView.as_view(), name="agent-validate"),
    path("agents/login/", AgentLoginView.as_view(), name="agent-login"),
    path(
        "agents/<str:code>/calltools/",
        CallToolsAgentStatusView.as_view(),
        name="agent-calltools-status",
    ),
    # CallTools dialer
    path("calltools/campaigns/", CallToolsCampaignsView.as_view(), name="calltools-campaigns"),
    # User
    path("me/", MeView.as_view(), name="me"),
    # Misc
    path("health/", HealthView.as_view(), name="health"),
    # Domain resources
    path("", include(router.urls)),
]
