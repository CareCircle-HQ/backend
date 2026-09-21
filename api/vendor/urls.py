"""URLs for the vendor API.

This module is NEVER included by ``backend/urls.py``. It is reachable only via
:class:`api.middleware.VendorHostMiddleware`, which swaps ``request.urlconf`` on
the vendor hostname -- so these routes are absent from the main site and the CRM's
routes are absent from here. A CRM path on this host is a 404 because it does not
exist, not because it is forbidden.
"""
from django.urls import path

from .views import (
    VendorAssessmentSaveView,
    VendorCompanyView,
    VendorDashboardView,
    VendorPhotoView,
    VendorLoginView,
    VendorLogoView,
    VendorLogoutView,
    VendorMeView,
    VendorWorkDetailView,
    VendorRevealPhoneView,
    VendorScheduleView,
    VendorSignatureView,
    VendorSubmitAssessmentView,
    VendorTeamDetailView,
    VendorTeamListView,
    VendorWorkListView,
)

urlpatterns = [
    path("v1/auth/login/", VendorLoginView.as_view(), name="vendor-login"),
    path("v1/auth/logout/", VendorLogoutView.as_view(), name="vendor-logout"),
    path(
        "v1/company/", VendorCompanyView.as_view(), name="vendor-company",
    ),
    path(
        "v1/company/logo/", VendorLogoView.as_view(), name="vendor-company-logo",
    ),
    path("v1/me/", VendorMeView.as_view(), name="vendor-me"),
    path("v1/dashboard/", VendorDashboardView.as_view(), name="vendor-dashboard"),
    path("v1/team/", VendorTeamListView.as_view(), name="vendor-team-list"),
    path(
        "v1/team/<uuid:user_id>/",
        VendorTeamDetailView.as_view(), name="vendor-team-detail",
    ),
    path("v1/work/", VendorWorkListView.as_view(), name="vendor-work-list"),
    path(
        "v1/work/<uuid:order_id>/assessment/",
        VendorAssessmentSaveView.as_view(), name="vendor-assessment-save",
    ),
    path(
        "v1/work/<uuid:order_id>/photos/",
        VendorPhotoView.as_view(), name="vendor-photos",
    ),
    path(
        "v1/work/<uuid:order_id>/signatures/",
        VendorSignatureView.as_view(), name="vendor-signatures",
    ),
    path(
        "v1/work/<uuid:order_id>/submit/",
        VendorSubmitAssessmentView.as_view(), name="vendor-submit",
    ),
    path(
        "v1/work/<uuid:order_id>/reveal-phone/",
        VendorRevealPhoneView.as_view(), name="vendor-reveal-phone",
    ),
    path(
        "v1/work/<uuid:order_id>/schedule/",
        VendorScheduleView.as_view(), name="vendor-schedule",
    ),
    path(
        "v1/work/<uuid:order_id>/",
        VendorWorkDetailView.as_view(), name="vendor-work-detail",
    ),
]
