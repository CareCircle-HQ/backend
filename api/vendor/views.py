"""Vendor API views.

Every query is scoped to ``request.user.vendor``. That is not a filter to remember
at each call site -- it is why the principal carries the vendor at all.

PII MINIMISATION IS ENFORCED HERE, in ``_member_block``. The device receives the
member's name, phone, address and address notes, and NOTHING else: no Medicaid ID,
no date of birth, no Medicaid plan. Those still appear on the generated PDF because
the SERVER renders it, so the vendor's paperwork is complete while their phone --
an unmanaged device that holds data offline -- never receives the most sensitive
fields.

This is deliberately LESS than the vendor's current tool shows. Their existing
cover sheets print Medicaid ID and plan.
"""
import logging

from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import DispatchKind, DispatchOrder, DispatchStatus
from .auth import (
    IsVendorUser, VendorAuthentication, authenticate_user, client_ip, issue_token,
)

logger = logging.getLogger(__name__)


def error(code, detail, status_code=http.HTTP_400_BAD_REQUEST, **extra):
    """Machine-readable error body: the app branches on ``code``, not on prose."""
    body = {"error": code, "detail": detail}
    body.update(extra)
    return Response(body, status=status_code)


class VendorAPIView(APIView):
    """Base: vendor-only auth, and nothing inherited from the CRM's defaults."""

    authentication_classes = [VendorAuthentication]
    permission_classes = [IsVendorUser]


# ── auth ─────────────────────────────────────────────────────────────────────

class VendorLoginView(APIView):
    """POST /v1/auth/login/ -- email + password for an opaque bearer token.

    The only unauthenticated endpoint on this surface.
    """

    authentication_classes = []
    permission_classes = []
    # Throttled as the anonymous scope: this is on the public internet and is the
    # one endpoint worth brute-forcing.
    throttle_scope = "anon"

    def post(self, request):
        data = request.data or {}
        email = (data.get("email") or "").strip()
        password = data.get("password") or ""
        if not email or not password:
            return error("missing_credentials", "Email and password are required.")

        user = authenticate_user(email, password)
        if user is None:
            # One message for every failure mode. A different answer for "no such
            # user" would let anyone enumerate vendor staff.
            return error(
                "invalid_credentials", "Email or password is incorrect.",
                http.HTTP_401_UNAUTHORIZED,
            )

        raw, token = issue_token(
            user,
            ip=client_ip(request),
            device_label=(data.get("device_label") or "").strip(),
        )
        return Response({
            "access_token": raw,
            "expires_at": token.expires_at,
            "user": {
                "id": str(user.vendor_user_id),
                "name": user.name,
                "email": user.email,
                "is_admin": user.is_admin,
            },
            "vendor": {"id": str(user.vendor_id), "name": user.vendor.name},
        })


class VendorLogoutView(VendorAPIView):
    """POST /v1/auth/logout/ -- revoke the presented token.

    Revocation rather than expiry: a vendor handing a shared tablet to a colleague
    must be able to end their session now, and "lost my phone" has to be a
    two-second fix.
    """

    def post(self, request):
        token = request.user.token
        type(token).objects.filter(pk=token.pk, revoked_at=None).update(
            revoked_at=timezone.now(),
        )
        return Response({"ok": True})


class VendorMeView(VendorAPIView):
    """GET /v1/me/ -- who am I, and which company am I scoped to."""

    def get(self, request):
        p = request.user
        return Response({
            "user": {
                "id": str(p.vendor_user.vendor_user_id),
                "name": p.vendor_user.name,
                "email": p.vendor_user.email,
                "is_admin": p.is_vendor_admin,
            },
            "vendor": {"id": str(p.vendor_id), "name": p.vendor.name},
            "token_expires_at": p.token.expires_at,
        })


# ── work ─────────────────────────────────────────────────────────────────────

def _member_block(order):
    """The ONLY member fields a vendor device receives.

    Name, phone, address, notes. Medicaid ID, date of birth and Medicaid plan are
    deliberately absent -- see the module docstring. The address comes from the
    ORDER, not live from the client record, so it is the address that was verified
    once for this assessment.
    """
    client = order.client
    first = (client.first_name or "").strip()
    last = (client.last_name or "").strip()
    return {
        "name": f"{first} {last}".strip(),
        "phone": order.contact_phone,
        "phone_type": order.contact_phone_type,
        "address": order.service_address,
        "address_notes": order.address_notes,
    }


def _order_block(order):
    """One assignment, as the app lists it."""
    return {
        "id": str(order.dispatch_order_id),
        "kind": order.kind,
        "kind_label": order.get_kind_display(),
        "status": order.status,
        "status_label": order.get_status_display(),
        "referral_type": order.referral_type,
        "location": order.location,
        "member": _member_block(order),
        # What the member offered. The vendor picks from these to confirm an
        # appointment; it is not itself an appointment.
        "availability": [
            {"date": w.date, "start_time": w.start_time, "end_time": w.end_time}
            for w in order.availability_windows.all()
        ],
        "visits": [
            {
                "scheduled_for": v.scheduled_for,
                "confirmed_at": v.confirmed_at,
                "started_at": v.started_at,
                "completed_at": v.completed_at,
            }
            for v in order.visits.all()
        ],
        # Work orders carry the items to install; an assessment carries none.
        "items": [
            {"item": i.item, "location": i.location, "qty": 1}
            for i in order.line_items.all()
        ],
        "created_at": order.created_at,
    }


class VendorWorkListView(VendorAPIView):
    """GET /v1/work/ -- this vendor's open assignments.

    Scoped to ``request.user.vendor``, so a vendor cannot see another's work even
    by guessing an id. Terminal orders are excluded by default: an installer opens
    this to find what to do next, not to browse history.
    """

    def get(self, request):
        include_done = (request.query_params.get("include_done") or "").lower() in (
            "1", "true", "yes",
        )
        qs = (
            DispatchOrder.objects
            .filter(vendor=request.user.vendor)
            .select_related("client")
            .prefetch_related("availability_windows", "visits", "line_items")
        )
        if not include_done:
            qs = qs.exclude(
                status__in=[DispatchStatus.UPLOADED, DispatchStatus.CANCELLED],
            )
        # Assessments first -- a work order cannot be done before the assessment
        # that justified it -- then oldest first, because the longest-waiting
        # member should be visited soonest.
        qs = qs.order_by("kind", "created_at")
        return Response([_order_block(o) for o in qs])


class VendorWorkDetailView(VendorAPIView):
    """GET /v1/work/<order_id>/ -- one assignment.

    404 rather than 403 for another vendor's order: a 403 would confirm the id
    exists.
    """

    def get(self, request, order_id):
        order = (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor)
            .select_related("client")
            .prefetch_related("availability_windows", "visits", "line_items")
            .first()
        )
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        payload = _order_block(order)
        # The assessment form, for an assessment order. Read from the same
        # template the CRM renders, so the two cannot drift.
        if order.kind == DispatchKind.ASSESSMENT:
            from ..services.assessment_forms import (
                build_schema, modules_for_referral,
            )

            form = getattr(order, "questionnaire", None)
            payload["form"] = {
                "state": form.state if form else "not_started",
                "template_version": form.template_version if form else None,
                "schema": (
                    form.schema() if form
                    else build_schema(modules_for_referral(order.referral_type))
                ),
                "answers": form.answers if form else {},
                "section_other": form.section_other if form else {},
                "interventions": form.interventions if form else [],
                "justification": form.justification if form else "",
                "assessor_notes": form.assessor_notes if form else "",
            }
        return Response(payload)
