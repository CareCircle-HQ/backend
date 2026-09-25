"""The member mobile-app URLConf.

⚠ THIS IS THE WHOLE SURFACE. It is swapped in by ``MemberAppHostMiddleware`` on
``MEMBER_API_HOST``, which means no CRM route, no extension route and no portal route
EXISTS on that hostname -- ``/api/clients/`` there is a 404, not a 403.

Anything added to this file is reachable from a member's phone. Nothing else is.
"""
from django.urls import path

from .views import (
    MemberLoginView, MemberLogoutView, MemberMeView, MemberPasswordView,
    MemberVerifyCodeView,
)
from .views import (
    MemberBenefitsView, MemberDashboardView, MemberDeliveriesView,
    MemberAssessmentView, MemberDeliveryHistoryView, MemberDeliveryPhotosView,
)

urlpatterns = [
    path("v1/auth/login/", MemberLoginView.as_view(), name="member-app-login"),
    path(
        "v1/auth/verify-code/",
        MemberVerifyCodeView.as_view(), name="member-app-verify-code",
    ),
    path("v1/auth/logout/", MemberLogoutView.as_view(), name="member-app-logout"),
    path("v1/me/", MemberMeView.as_view(), name="member-app-me"),
    path("v1/me/password/", MemberPasswordView.as_view(), name="member-app-password"),
    path(
        "v1/me/dashboard/",
        MemberDashboardView.as_view(), name="member-app-dashboard",
    ),
    path(
        "v1/me/assessment/",
        MemberAssessmentView.as_view(), name="member-app-assessment",
    ),
    path(
        "v1/me/benefits/",
        MemberBenefitsView.as_view(), name="member-app-benefits",
    ),
    path(
        "v1/me/deliveries/",
        MemberDeliveriesView.as_view(), name="member-app-deliveries",
    ),
    path(
        "v1/me/deliveries/history/",
        MemberDeliveryHistoryView.as_view(), name="member-app-delivery-history",
    ),
    path(
        "v1/me/deliveries/<uuid:delivery_id>/photos/",
        MemberDeliveryPhotosView.as_view(), name="member-app-delivery-photos",
    ),
]
