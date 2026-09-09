"""URLConf for the delivery-partner host -- the ONLY routes vendors can reach.

Selected per-request by ``api.middleware.PartnerHostMiddleware`` when the Host
matches ``settings.PARTNER_API_HOST``. Crucially, ``backend/urls.py`` never
includes this module and this module never includes the CRM's: the two surfaces
are mutually invisible, so a CRM path on the partner host is a 404 (the route
does not exist) rather than a permission decision that could be misconfigured.

Do NOT add anything here that reads CRM data beyond the caller's own orders.
"""

from django.http import JsonResponse
from django.urls import path

from .views import (
    DeliveryStatusView,
    ProofBase64View,
    ProofConfirmView,
    ProofPresignView,
    ProofUploadView,
    TokenView,
    WhoAmIView,
)


def not_found(request, *args, **kwargs):
    """Anything else on this host: a plain JSON 404 with no CRM detail."""
    return JsonResponse({"error": "not_found", "detail": "Unknown endpoint."}, status=404)


urlpatterns = [
    path("v1/token/", TokenView.as_view(), name="partner-token"),
    path("v1/whoami/", WhoAmIView.as_view(), name="partner-whoami"),
    path(
        "v1/deliveries/<uuid:order_id>/status/",
        DeliveryStatusView.as_view(),
        name="partner-delivery-status",
    ),
    path(
        "v1/deliveries/<uuid:order_id>/proofs/",
        ProofUploadView.as_view(),
        name="partner-proof-upload",
    ),
    path(
        "v1/deliveries/<uuid:order_id>/proofs/base64/",
        ProofBase64View.as_view(),
        name="partner-proof-base64",
    ),
    path(
        "v1/deliveries/<uuid:order_id>/proofs/presign/",
        ProofPresignView.as_view(),
        name="partner-proof-presign",
    ),
    path(
        "v1/deliveries/<uuid:order_id>/proofs/confirm/",
        ProofConfirmView.as_view(),
        name="partner-proof-confirm",
    ),
]

# No catch-all: an unmatched path raises the normal 404. ``not_found`` is exposed
# for handler404 if the partner host ever needs a JSON-only error page.
handler404 = not_found
