from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from .views import (
    CaseViewSet,
    ClientViewSet,
    EligibilityViewSet,
    HealthView,
    ImportBatchViewSet,
    MeView,
    ProgramViewSet,
    ProviderViewSet,
    RegisterView,
    ScreeningViewSet,
)

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
router.register("cases", CaseViewSet, basename="case")
router.register("screenings", ScreeningViewSet, basename="screening")
router.register("eligibility", EligibilityViewSet, basename="eligibility")
router.register("providers", ProviderViewSet, basename="provider")
router.register("programs", ProgramViewSet, basename="program")
router.register("import-batches", ImportBatchViewSet, basename="import-batch")

urlpatterns = [
    # Auth
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # User
    path("me/", MeView.as_view(), name="me"),
    # Misc
    path("health/", HealthView.as_view(), name="health"),
    # Domain resources
    path("", include(router.urls)),
]
