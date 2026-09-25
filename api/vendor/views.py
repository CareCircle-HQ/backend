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
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.views import APIView

from ..models import (
    DispatchKind, DispatchOrder, DispatchStatus, DispatchVisit, VendorUser,
)
from .auth import (
    IsVendorAdmin, IsVendorUser, VendorAuthentication, authenticate_user,
    client_ip, issue_token,
)

# ⚠ MODULE LEVEL, deliberately. This was imported inside a dozen methods, and a
# helper that needed it (_order_block) had no import of its own -- so adding one
# call there raised NameError at request time with every test passing, because
# the tests that exercise that path do not assert on a 500. There is no cycle:
# services.dispatch does not import the vendor views.
from ..services import dispatch as dispatch_svc

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
    # Contact details are INHERITED by a work order, like the address: the wizard
    # collects them once on the assessment, so a work order carries none of its
    # own and an installer would otherwise see no number to ring.
    phone, phone_type, notes = order.service_contact
    return {
        "name": f"{first} {last}".strip(),
        # MASKED by default. The full number is a separate, RECORDED request --
        # see VendorRevealPhoneView. The last four digits are shown because a
        # vendor needs to confirm they have the right person before ringing, and
        # that much does not identify anybody on its own.
        "phone_masked": _mask_phone(phone),
        "phone_last4": "".join(c for c in phone if c.isdigit())[-4:],
        "has_phone": bool(phone.strip()),
        "phone_type": phone_type,
        "address": order.service_address,
        "address_notes": notes,
    }


def _mask_phone(phone):
    """``(305) 781-3277`` -> ``(•••) •••-3277``.

    Keeps the SHAPE so it reads as a phone number rather than a redaction, and
    keeps the last four so the vendor can check they are looking at the right
    member before asking to see the rest.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 4:
        return ""
    return f"(•••) •••-{digits[-4:]}"


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
        # The governing Dwelling Assessment case, in FULL. A vendor quotes this
        # number on paperwork, and a truncated id cannot be quoted.
        "dwelling_case_id": (
            str(order.case_id) if order.case_id
            else str(order.parent.case_id) if order.parent_id and order.parent.case_id
            else ""
        ),
        # The programme name, so the app can head the screen with the real service
        # ("Environmental Exposure Assessment") rather than our internal kind.
        "program_name": (
            order.case.program_name if order.case_id
            else order.parent.case.program_name
            if order.parent_id and order.parent.case_id else ""
        ),
        "member": _member_block(order),
        # What the member offered. The vendor picks from these to confirm an
        # appointment; it is not itself an appointment.
        "availability": [
            {"date": w.date, "start_time": w.start_time, "end_time": w.end_time}
            # Scheduling windows only -- see dispatch.scheduling_windows.
            for w in dispatch_svc.scheduling_windows(order)
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
        params = request.query_params
        qs = (
            DispatchOrder.objects
            .filter(vendor=request.user.vendor)
            .select_related("client", "case", "parent", "parent__case")
            .prefetch_related("availability_windows", "visits", "line_items")
        )


        # ?status=confirmed&status=submitted -- repeatable, so the app's filter
        # chips are multi-select without inventing a comma syntax. An unknown
        # value is IGNORED rather than 400: a stale app version sending a status
        # we have retired should show everything, not fail.
        wanted = [
            v for v in params.getlist("status")
            if v in DispatchStatus.values
        ]
        if wanted:
            qs = qs.filter(status__in=wanted)
        elif (params.get("include_done") or "").lower() not in ("1", "true", "yes"):
            # Default: the work still to do. An installer opens this to find what
            # is next, not to browse history.
            qs = qs.exclude(
                status__in=[DispatchStatus.UPLOADED, DispatchStatus.CANCELLED],
            )

        kind = (params.get("kind") or "").strip()
        if kind in DispatchKind.values:
            qs = qs.filter(kind=kind)

        # Assessments first -- a work order cannot be done before the assessment
        # that justified it -- then oldest first, because the longest-waiting
        # member should be visited soonest.
        qs = qs.order_by("kind", "created_at")

        # WITHHELD, LAST: an order whose dwelling is outside our service area is not
        # sent, however it got created. A vendor cannot service an address we do not
        # cover, and the trip is billable whether or not the visit was possible.
        #
        # Applied here rather than at creation, because the address can be CORRECTED
        # afterwards -- a withheld order appears the moment an agent fixes the ZIP,
        # with no second action needed. And applied after the queryset is complete,
        # because it returns a list: doing it earlier broke the status filters that
        # follow.
        #
        # In Python, not SQL: the answer depends on the ServiceZipCode whitelist and,
        # for a work order, on its PARENT's address. An open list is a handful of
        # rows, which is cheaper than making the rule expressible twice.

        return Response([
            _order_block(o) for o in qs if dispatch_svc.is_dispatchable(o)
        ])


class VendorWorkDetailView(VendorAPIView):
    """GET /v1/work/<order_id>/ -- one assignment.

    404 rather than 403 for another vendor's order: a 403 would confirm the id
    exists.
    """

    def get(self, request, order_id):
        order = (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor)
            .select_related("client", "case", "parent", "parent__case")
            .prefetch_related("availability_windows", "visits", "line_items")
            .first()
        )
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        # A WITHHELD order answers 404 as well, for the same reason the list omits
        # it: it was never sent. 404 rather than an explanation, because the vendor
        # is not the person who can fix a service-area problem and the dwelling's
        # ZIP is not theirs to know. Logged so WE can see what was withheld.

        if not dispatch_svc.is_dispatchable(order):
            logger.info(
                "withheld order %s from vendor %s: %s",
                order.pk, request.user.vendor_id,
                dispatch_svc.not_dispatchable_reason(order),
            )
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        payload = _order_block(order)
        # The assessment form, for an assessment order. Read from the same
        # template the CRM renders, so the two cannot drift.
        if order.kind == DispatchKind.ASSESSMENT:
            from ..services.assessment_forms import (
                build_schema, modules_for_referral,
            )
            from ..services import pricing

            form = getattr(order, "questionnaire", None)
            schema = (
                form.schema() if form
                else build_schema(modules_for_referral(order.referral_type))
            )
            # THIS VENDOR's price on each intervention option, so they can see what
            # they will invoice us while choosing quantities. Their negotiated price
            # where one exists, otherwise the base.
            #
            # The vendor sees ONLY their own price -- never the admin fee or what we
            # bill Unite Us. That is our margin, and putting it on their device
            # would be handing a supplier our mark-up.
            # In pricing.apply_vendor_prices so the WALK can be tested against a
            # literal schema -- this used to iterate a key the schema does not have,
            # silently pricing nothing and leaving the funding cap unable to trip.
            pricing.apply_vendor_prices(schema, order.vendor)

            # THE CAP, as flags and a budget the app can compute against -- never
            # as a running total we render. The app already holds each option's
            # price, so it can warn the instant a quantity changes without a round
            # trip; what it must not do is put a dollar figure on a screen in the
            # member's living room.
            cap = pricing.cap_status(
                order.vendor, form.interventions if form else [],
            )
            payload["cap"] = {
                "at_cap": cap["at_cap"],
                "over_cap": cap["over_cap"],
                # The budget and the assessment's share, so the app can recompute
                # locally as the vendor taps. Both are OUR figures, not the
                # member's, and the app renders neither.
                "limit": str(cap["cap"]),
                "assessment": str(cap["assessment"]),
            }
            payload["form"] = {
                "state": form.state if form else "not_started",
                "template_version": form.template_version if form else None,
                "schema": schema,
                "answers": form.answers if form else {},
                "section_other": form.section_other if form else {},
                "interventions": form.interventions if form else [],
                "justification": form.justification if form else "",
                "assessor_notes": form.assessor_notes if form else "",
            }
        return Response(payload)


class VendorDashboardView(VendorAPIView):
    """GET /v1/dashboard/ -- the numbers a vendor opens the app to see.

    Counts, not a list: the work list already does lists. These answer "what do I
    owe, and what is coming" at a glance, which is the only reason to look at a
    dashboard before starting a day.

    Every count is scoped to ``request.user.vendor``.
    """

    def get(self, request):
        from django.db.models import Count, Q

        vendor = request.user.vendor
        today = timezone.localdate()
        week_end = today + timezone.timedelta(days=7)

        base = DispatchOrder.objects.filter(vendor=vendor)

        # One query for the status spread rather than six counts.
        by_status = {
            row["status"]: row["n"]
            for row in base.values("status").annotate(n=Count("pk"))
        }
        by_kind = {
            row["kind"]: row["n"]
            for row in base.exclude(
                status__in=[DispatchStatus.UPLOADED, DispatchStatus.CANCELLED],
            ).values("kind").annotate(n=Count("pk"))
        }

        # Visits, which is what "today" and "this week" actually mean to an
        # installer -- an order's status says nothing about when they must travel.
        visits = DispatchVisit.objects.filter(
            dispatch_order__vendor=vendor,
            completed_at__isnull=True,
        ).select_related("dispatch_order", "dispatch_order__client")

        today_visits = visits.filter(scheduled_for__date=today)
        week_visits = visits.filter(
            scheduled_for__date__gt=today, scheduled_for__date__lte=week_end,
        )
        # Scheduled, in the past, never started: the queue that quietly rots.
        # Worth its own number because no status transition marks it.
        overdue = visits.filter(
            scheduled_for__date__lt=today, started_at__isnull=True,
        )

        next_visit = (
            visits.filter(scheduled_for__gte=timezone.now())
            .order_by("scheduled_for").first()
        )

        return Response({
            "vendor": {"id": str(vendor.vendor_id), "name": vendor.name},
            "open": {
                # The three states a vendor can act on, named for what to DO
                # rather than for the status value.
                "needs_scheduling": by_status.get(
                    DispatchStatus.PENDING_SCHEDULE, 0,
                ),
                "confirmed": by_status.get(DispatchStatus.CONFIRMED, 0),
                "awaiting_submission": by_status.get(
                    DispatchStatus.PENDING_SUBMISSION, 0,
                ),
            },
            "done": {
                "submitted": by_status.get(DispatchStatus.SUBMITTED, 0),
                "uploaded": by_status.get(DispatchStatus.UPLOADED, 0),
            },
            "open_by_kind": {
                "assessment": by_kind.get(DispatchKind.ASSESSMENT, 0),
                "remediation": by_kind.get(DispatchKind.REMEDIATION, 0),
            },
            "visits": {
                "today": today_visits.count(),
                "next_7_days": week_visits.count(),
                "overdue": overdue.count(),
            },
            "next_visit": (
                {
                    "order_id": str(next_visit.dispatch_order_id),
                    "scheduled_for": next_visit.scheduled_for,
                    "member_name": (
                        f"{next_visit.dispatch_order.client.first_name} "
                        f"{next_visit.dispatch_order.client.last_name}"
                    ).strip(),
                    "kind": next_visit.dispatch_order.kind,
                }
                if next_visit else None
            ),
        })


# ── the team ─────────────────────────────────────────────────────────────────

class VendorAdminAPIView(VendorAPIView):
    """Base for endpoints only the vendor's administrator may call."""

    permission_classes = [IsVendorUser, IsVendorAdmin]


def _team_member(user):
    return {
        "id": str(user.vendor_user_id),
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
    }


class VendorTeamListView(VendorAdminAPIView):
    """GET / POST /v1/team/ -- the company's staff.

    Scoped to the caller's vendor, so an administrator cannot see or touch another
    company's people even by guessing an id.
    """

    def get(self, request):
        users = (
            VendorUser.objects
            .filter(vendor=request.user.vendor)
            # The administrator first, then alphabetically -- a list that starts
            # with "who runs this account" reads better than one that starts with
            # whoever happens to sort first.
            .order_by("-is_admin", "name")
        )
        return Response([_team_member(u) for u in users])

    def post(self, request):
        from django.contrib.auth.hashers import make_password
        from django.utils.crypto import get_random_string

        data = request.data or {}
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip().lower()
        if not name or not email:
            return error("missing_fields", "Name and email are required.")

        # Unique across ALL vendors, because email is the login identifier. Answer
        # the same way whether the clash is inside this company or another one:
        # "already exists elsewhere" would leak that a person works for a
        # competitor.
        if VendorUser.objects.filter(email__iexact=email).exists():
            return error(
                "email_taken", "That email is already in use.",
                http.HTTP_409_CONFLICT,
            )

        # Opaque, but blank-after-trim still means "generate one" -- see the note
        # in portal/views_settings.py admin_user.
        _raw = data.get("password") or ""
        supplied = _raw if _raw.strip() else ""
        if supplied and len(supplied) < 8:
            return error(
                "password_too_short", "Password must be at least 8 characters.",
            )
        password = supplied or get_random_string(14)

        user = VendorUser.objects.create(
            vendor=request.user.vendor,
            name=name,
            email=email,
            phone=(data.get("phone") or "").strip(),
            password=make_password(password),
            # NEVER an admin. The admin slot is CRM-provisioned and constrained to
            # one per vendor, so a company cannot grow a second administrator --
            # which is what keeps "who can add people" answerable.
            is_admin=False,
        )
        logger.info(
            "vendor team: %s added %s (%s) at %s",
            request.user.vendor_user.email, email, user.vendor_user_id,
            request.user.vendor.name,
        )
        payload = _team_member(user)
        # Echoed only when WE generated it -- an admin who typed the password
        # already has it, and repeating it puts a secret they chose in the
        # response body.
        payload["temporary_password"] = "" if supplied else password
        payload["password_was_supplied"] = bool(supplied)
        return Response(payload, status=http.HTTP_201_CREATED)


class VendorTeamDetailView(VendorAdminAPIView):
    """PATCH / DELETE /v1/team/<user_id>/ -- change or remove a colleague."""

    def _get(self, request, user_id):
        return VendorUser.objects.filter(
            pk=user_id, vendor=request.user.vendor,
        ).first()

    def patch(self, request, user_id):
        user = self._get(request, user_id)
        if user is None:
            # 404 rather than 403: a 403 would confirm the id exists.
            return error("not_found", "No such user.", http.HTTP_404_NOT_FOUND)

        data = request.data or {}
        changed = []

        if "is_active" in data:
            active = bool(data["is_active"])
            if user.is_admin and not active:
                # Deactivating the only administrator locks the company out of its
                # own account, and only the CRM could undo it.
                return error(
                    "cannot_deactivate_admin",
                    "The administrator account cannot be deactivated here. "
                    "Ask CareCircle to do it.",
                )
            if user.is_active != active:
                user.is_active = active
                changed.append("is_active")

        for field in ("name", "phone"):
            if field in data:
                value = (data.get(field) or "").strip()
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    changed.append(field)

        if "password" in data:
            from django.contrib.auth.hashers import make_password

            # Opaque, but blank-after-trim still means "generate one" -- see the
            # note in portal/views_settings.py admin_user.
            _raw = data.get("password") or ""
            supplied = _raw if _raw.strip() else ""
            if len(supplied) < 8:
                return error(
                    "password_too_short",
                    "Password must be at least 8 characters.",
                )
            user.password = make_password(supplied)
            changed.append("password")

        if changed:
            user.save()
            logger.info(
                "vendor team: %s changed %s on %s",
                request.user.vendor_user.email, sorted(changed), user.email,
            )
        payload = _team_member(user)
        # Reported so the app can say "nothing to save" rather than appearing to
        # have saved -- the same gap that made a CRM edit look successful while
        # leaving no trace.
        payload["changed_fields"] = sorted(changed)
        return Response(payload)

    def delete(self, request, user_id):
        """Remove a colleague.

        DEACTIVATES rather than deletes when they have done any work, because
        submissions and signatures point at them and the record of who assessed a
        member's home must survive. A user who has never submitted anything is
        deleted outright -- an email typed wrong five minutes ago should not be
        permanent.

        Deactivation takes effect immediately: the authenticator re-checks
        is_active on EVERY request, so an existing token stops working at once
        rather than lasting until it expires.
        """
        user = self._get(request, user_id)
        if user is None:
            return error("not_found", "No such user.", http.HTTP_404_NOT_FOUND)
        if user.is_admin:
            return error(
                "cannot_remove_admin",
                "The administrator account cannot be removed here. "
                "Ask CareCircle to do it.",
            )
        if user.pk == request.user.vendor_user.pk:
            # Belt and braces: the admin check above already covers today's only
            # admin, but removing yourself should never be possible.
            return error("cannot_remove_self", "You cannot remove yourself.")

        has_history = (
            user.dispatch_submissions.exists()
            or user.dispatch_submissions_voided.exists()
            or user.dispatch_documents.exists()
        )
        if has_history:
            user.is_active = False
            user.save(update_fields=["is_active", "updated_at"])
            logger.info(
                "vendor team: %s deactivated %s (has history)",
                request.user.vendor_user.email, user.email,
            )
            return Response({
                "removed": False,
                "deactivated": True,
                "detail": (
                    "This user has submitted work, so their access was removed but "
                    "their record is kept."
                ),
            })

        email = user.email
        user.delete()
        logger.info(
            "vendor team: %s deleted %s (no history)",
            request.user.vendor_user.email, email,
        )
        return Response({"removed": True, "deactivated": False})


# ── scheduling ───────────────────────────────────────────────────────────────

class VendorScheduleView(VendorAPIView):
    """GET / POST /v1/work/<order_id>/schedule/

    GET returns what the vendor needs to choose a time: the windows the member
    offered, and the vendor's OWN appointments on each of those days. Those two
    together are the whole decision, and fetching them separately would let the
    list disagree with the overlap check.

    POST books it, texts the member and sets the reminders.
    """

    def _order(self, request, order_id):
        return (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor)
            .select_related("client", "vendor")
            .prefetch_related("availability_windows", "visits")
            .first()
        )

    def get(self, request, order_id):
        from ..services import scheduling

        order = self._order(request, order_id)
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        # ⚠ PURPOSE-FILTERED. An assessment carries the MEMBER's availability AND,
        # once submitted, the vendor's tentative INSTALL windows -- reading them all
        # would present the install offers as member availability.
        windows = dispatch_svc.scheduling_windows(order)
        # The vendor's existing appointments on each offered day, so the app can
        # warn before they pick rather than after they submit.
        dates = sorted({w.date for w in windows})
        extra = (request.query_params.get("date") or "").strip()
        if extra:
            from datetime import date as _date

            try:
                parsed = _date.fromisoformat(extra)
                if parsed not in dates:
                    dates.append(parsed)
            except ValueError:
                pass

        busy = {}
        for day in dates:
            busy[day.isoformat()] = [
                {
                    "order_id": str(v.dispatch_order_id),
                    "member_name": (
                        f"{v.dispatch_order.client.first_name} "
                        f"{v.dispatch_order.client.last_name}"
                    ).strip(),
                    "starts_at": v.scheduled_for,
                    "ends_at": v.scheduled_end,
                }
                for v in scheduling.day_appointments(
                    request.user.vendor, day, exclude_order=order,
                )
            ]

        visit = order.visits.first()
        return Response({
            "order_id": str(order.dispatch_order_id),
            "status": order.status,
            "member_name": (
                f"{order.client.first_name} {order.client.last_name}"
            ).strip(),
            # Stated so the app can label the screen rather than guess. Every time
            # in this payload is the MEMBER's local time -- they are the one waiting
            # at home.
            "timezone": str(timezone.get_default_timezone()),
            # service_consent so a work order reflects the consent recorded on its
            # assessment rather than its own always-False flag.
            "consent_to_text": order.service_consent[1],
            "availability": [
                {
                    "id": w.pk,
                    "date": w.date,
                    "start_time": w.start_time,
                    "end_time": w.end_time,
                }
                for w in windows
            ],
            "appointments_by_date": busy,
            "allowed_durations": list(scheduling.ALLOWED_DURATIONS),
            "default_duration": scheduling.DEFAULT_DURATION,
            "current": (
                {
                    "starts_at": visit.scheduled_for,
                    "ends_at": visit.scheduled_end,
                    "confirmed_at": visit.confirmed_at,
                }
                if visit and visit.scheduled_for else None
            ),
        })

    def post(self, request, order_id):
        from datetime import date as _date, time as _time

        from ..services import scheduling

        order = self._order(request, order_id)
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        data = request.data or {}
        try:
            date_value = _date.fromisoformat((data.get("date") or "").strip())
        except ValueError:
            return error("bad_date", "A valid date is required (YYYY-MM-DD).")
        raw_time = (data.get("arrival_time") or "").strip()
        try:
            # Accept "09:00" and "09:00:00" -- an <input type=time> sends the
            # former and our own API returns the latter.
            arrival = _time.fromisoformat(raw_time)
        except ValueError:
            return error("bad_time", "A valid arrival time is required (HH:MM).")

        try:
            duration = int(data.get("duration_minutes") or scheduling.DEFAULT_DURATION)
        except (TypeError, ValueError):
            return error("bad_duration", "Session length must be a number of minutes.")

        try:
            visit, message = scheduling.schedule_visit(
                order,
                date_value=date_value,
                arrival_time=arrival,
                duration_minutes=duration,
                vendor_user=request.user.vendor_user,
                notes=(data.get("notes") or "").strip(),
            )
        except scheduling.SchedulingError as exc:
            return error(exc.code, str(exc))

        order.refresh_from_db()
        return Response({
            "order_id": str(order.dispatch_order_id),
            "status": order.status,
            "status_label": order.get_status_display(),
            "visit": {
                "starts_at": visit.scheduled_for,
                "ends_at": visit.scheduled_end,
                "confirmed_at": visit.confirmed_at,
            },
            # Whether the member was actually told, and if not, why. The vendor has
            # to know: a booking the member never heard about is a wasted journey,
            # and silence here would hide that.
            "member_notified": message.status not in ("blocked", "failed"),
            "message": {
                "status": message.status,
                "reason": message.error_code,
                "detail": message.error_detail,
                "to": message.to_number,
                "body": message.body,
            },
            "reminders": [
                {
                    "kind": r.kind, "audience": r.audience, "send_at": r.send_at,
                }
                for r in visit.reminders.filter(
                    cancelled_at__isnull=True, sent_at__isnull=True,
                ).order_by("send_at")
            ],
        }, status=http.HTTP_201_CREATED)


class VendorRevealPhoneView(VendorAPIView):
    """POST /v1/work/<order_id>/reveal-phone/ -- the member's full number.

    A DELIBERATE, RECORDED action rather than a field on the order. The device is
    unmanaged and the number is the most directly identifying thing a vendor
    handles, so "who looked at this, and when" has to be answerable.

    Recorded on the CASE history (StageEvent against the dispatch order), NOT the
    member's timeline: the member did not do anything, and filling their history
    with vendor lookups would bury the events that describe their care.
    """

    def post(self, request, order_id):
        from ..models import StageEventSource

        order = (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor)
            .select_related("client", "vendor")
            .first()
        )
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        phone, phone_type, _notes = order.service_contact
        if not (phone or "").strip():
            return error("no_phone", "No phone number on this order.")

        user = request.user.vendor_user
        dispatch_svc.record_transition(
            order, order.status, order.status,
            source=StageEventSource.MANUAL,
            note="vendor viewed the member's phone number",
            metadata={
                # The value itself is NOT recorded. Logging the number to prove
                # someone looked at the number would spread it further than the
                # lookup did.
                "vendor_user": user.email,
                "vendor_user_name": user.name,
                "vendor": order.vendor.name if order.vendor_id else "",
            },
        )
        logger.info(
            "vendor phone reveal: order=%s vendor_user=%s", order.pk, user.email,
        )
        return Response({"phone": phone, "phone_type": phone_type})


# ── completing the assessment ────────────────────────────────────────────────

class VendorAssessmentSaveView(VendorAPIView):
    """PATCH /v1/work/<order_id>/assessment/ -- save the answers as a DRAFT.

    Idempotent and whole-document: the app sends everything it has, and this
    replaces it. A field-by-field merge would need the client and server to agree
    about which of them last touched each answer, and an assessor who unticks a box
    offline must see it unticked when they sync.

    Saving is NOT submitting. A draft has no gate and can be saved as often as the
    app likes.
    """

    def _questionnaire(self, request, order_id):
        from ..models import DispatchKind, DispatchOrder, DispatchQuestionnaire
        from ..services.assessment_forms import modules_for_referral

        order = (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor,
                    kind=DispatchKind.ASSESSMENT)
            .select_related("client", "vendor").first()
        )
        if order is None:
            return None, None
        form, _created = DispatchQuestionnaire.objects.get_or_create(
            dispatch_order=order,
            defaults={"modules": modules_for_referral(order.referral_type)},
        )
        return order, form

    def patch(self, request, order_id):
        from ..models import DispatchQuestionnaireState
        from ..services.assessment_forms import all_option_codes, all_question_codes

        order, form = self._questionnaire(request, order_id)
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        if form.state == DispatchQuestionnaireState.SUBMITTED:
            return error(
                "already_submitted",
                "This assessment has been submitted and cannot be edited.",
                http.HTTP_409_CONFLICT,
            )

        data = request.data or {}
        modules = form.modules or []

        if "answers" in data:
            # UNKNOWN CODES ARE DROPPED, not rejected. A stale app holding a
            # retired question should still be able to save the answers that do
            # exist -- failing the whole save would lose a completed visit over a
            # question nobody asks any more.
            known = set(all_question_codes(modules))
            form.answers = {
                k: bool(v) for k, v in (data.get("answers") or {}).items()
                if k in known
            }
        if "section_other" in data:
            form.section_other = {
                str(k): str(v)[:1000]
                for k, v in (data.get("section_other") or {}).items()
            }
        if "interventions" in data:
            known_options = set(all_option_codes(modules))
            cleaned = []
            for entry in (data.get("interventions") or []):
                code = (entry or {}).get("option")
                try:
                    qty = int((entry or {}).get("qty") or 0)
                except (TypeError, ValueError):
                    qty = 0
                # qty 0 means "not recommended", so it is simply not stored --
                # keeping zeros would make "what did they recommend?" a filter
                # rather than a read.
                if code in known_options and qty > 0:
                    cleaned.append({"option": code, "qty": qty})
            form.interventions = cleaned
        if "justification" in data:
            form.justification = (data.get("justification") or "").strip()
        if "assessor_notes" in data:
            form.assessor_notes = (data.get("assessor_notes") or "").strip()

        form.save()

        from ..services import pricing

        cap = pricing.cap_status(order.vendor, form.interventions)
        return Response({
            "state": form.state,
            # Recomputed server-side on every save, so the app's warning cannot
            # drift from the rule that will actually be enforced at submit.
            "cap": {
                "at_cap": cap["at_cap"], "over_cap": cap["over_cap"],
                "limit": str(cap["cap"]), "assessment": str(cap["assessment"]),
            },
            "answers": form.answers,
            "section_other": form.section_other,
            "interventions": form.interventions,
            "justification": form.justification,
            "assessor_notes": form.assessor_notes,
            "updated_at": form.updated_at,
        })


class VendorPhotoView(VendorAPIView):
    """POST /v1/work/<order_id>/photos/ -- attach a dwelling photo.

    Accepts multipart for now. The POD API's presign/confirm flow is the better
    shape at volume -- it keeps a 5 MB cellular upload off a gunicorn worker -- and
    this endpoint is deliberately compatible with moving to it: the response shape
    is the same either way.
    """

    parser_classes = [MultiPartParser, FormParser]
    # A phone photo after client-side compression is well under this; the ceiling
    # exists so an uncompressed 12 MP image fails with our JSON error rather than
    # an nginx HTML 413.
    MAX_BYTES = 12 * 1024 * 1024

    def post(self, request, order_id):
        import hashlib

        from ..models import DispatchOrder, DispatchProof
        from ..services import import_storage

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        files = request.FILES.getlist("file") or request.FILES.getlist("photo")
        if not files:
            return error("no_file", "Attach an image as 'file'.")

        # Which identified problem this evidences. Optional -- a general photo of
        # the dwelling has no category -- but a WRONG one is refused rather than
        # stored, because a typo would leave the gate permanently unsatisfiable and
        # the vendor unable to submit with no way to see why.
        # WHICH INSTALLED PRODUCT, on a work order. Validated against THIS order's
        # line items -- an id from another order would satisfy no gate here and would
        # quietly attach evidence to the wrong job.
        from ..models import DispatchItem
        item = None
        item_id = str(request.data.get("dispatch_item") or "").strip()
        if item_id:
            item = DispatchItem.objects.filter(
                dispatch_item_id=item_id, dispatch_order=order,
            ).first()
            if item is None:
                return error("bad_item", "That product is not on this work order.")

        from ..services.assessment_forms import FORMS

        group = (request.data.get("intervention_group") or "").strip()
        if group:
            # A QUESTION group code ("mob.risk.bathroom"), because the
            # questionnaires attach the photo requirement to the question group the
            # vendor just answered under -- not to a product category.
            valid = {
                g["code"] for sections in FORMS.values()
                for sec in sections for g in sec["groups"]
                if g.get("requires_photo")
            }
            if group not in valid:
                return error(
                    "bad_group",
                    f"'{group}' is not a section that requires a photo.",
                )

        created = []
        for upload in files:
            if upload.size > self.MAX_BYTES:
                return error(
                    "too_large",
                    f"{upload.name} is larger than 12 MB. Compress it first.",
                )
            raw = upload.read()
            digest = hashlib.sha256(raw).hexdigest()
            # Content-addressed and idempotent: an offline client retrying an upload
            # must not produce a second copy of the same photo.
            # Keyed on the HASH ALONE, matching the one_proof_per_hash_per_order
            # constraint. I first keyed it on (hash, category) so one wide shot
            # could evidence two problems, and the database refused it -- rightly.
            #
            # One image, one category is the stronger rule: if a single photo could
            # clear every category, the per-problem requirement would be theatre.
            # Taking a second photo while standing in the room is a trivial ask.
            existing = DispatchProof.objects.filter(
                dispatch_order=order, content_hash=digest,
            ).first()
            if existing is not None:
                if (existing.intervention_group == group
                        and existing.dispatch_item_id == (
                            item.dispatch_item_id if item else None
                        )):
                    # The same upload retried -- idempotent, as an offline client
                    # needs.
                    created.append(existing)
                    continue
                # ⚠ THE SAME IMAGE FOR A DIFFERENT PRODUCT. Without the item in the
                # comparison above this read as an idempotent retry, so the second
                # product silently kept no photo -- and since completion requires one
                # per product, the gate became unsatisfiable with nothing on screen
                # explaining why. One photo, one product, for the same reason one
                # photo evidences one problem: a single image that cleared every
                # product would make the requirement theatre.
                if item is not None or existing.dispatch_item_id is not None:
                    return error(
                        "photo_reused",
                        f"That photo is already attached to "
                        f"{existing.dispatch_item.item if existing.dispatch_item_id else 'another item'}"
                        f". Take a separate photo of "
                        f"{item.item if item else 'this one'}.",
                    )
                # The same image offered for a DIFFERENT problem. Said plainly,
                # because the alternative is a database error the vendor cannot act
                # on.
                labels = {
                    g["code"]: (g["label"] or sec["title"])
                    for sections in FORMS.values()
                    for sec in sections for g in sec["groups"]
                }
                where = labels.get(
                    existing.intervention_group, "the general photos",
                )
                return error(
                    "already_used",
                    f"That photo is already attached to {where}. "
                    f"Take a new photo for this one.",
                )
            key = import_storage.build_key(
                f"dispatch-proofs/{order.pk}/{digest[:16]}-{upload.name}"
            )
            import_storage.upload_bytes(
                key, raw, content_type=upload.content_type or "image/jpeg",
            )
            created.append(DispatchProof.objects.create(
                dispatch_order=order,
                # Null on an assessment photo: that evidences a PROBLEM, this
                # evidences a completed INSTALLATION.
                dispatch_item=item,
                s3_key=key,
                content_hash=digest,
                intervention_group=group,
                caption=(request.data.get("caption") or "").strip(),
                captured_at=timezone.now(),
            ))


        return Response({
            "photos": [
                {"id": p.pk, "content_hash": p.content_hash,
                 "intervention_group": p.intervention_group,
                 "captured_at": p.captured_at}
                for p in created
            ],
            "total": order.proofs.count(),
            # Returned on every upload so the app can tick a category off without a
            # second round trip, and cannot drift from the server's view of the gate.
            "missing_for_submission": dispatch_svc.missing_for_submission(order),
        }, status=http.HTTP_201_CREATED)


    def get(self, request, order_id):
        """What has already been photographed, by category.

        A visit interrupted and resumed -- or continued on a second device -- must
        not ask again for a photo the server already holds.
        """
        from ..models import DispatchOrder

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        return Response({
            "photos": [
                {"id": p.pk, "intervention_group": p.intervention_group,
                 "caption": p.caption, "captured_at": p.captured_at}
                for p in order.proofs.order_by("received_at")
            ],
            "missing_for_submission": dispatch_svc.missing_for_submission(order),
        })


class VendorSignatureView(VendorAPIView):
    """POST /v1/work/<order_id>/signatures/ -- store a drawn signature.

    The canvas arrives as a base64 data URL. Stored like a proof: an S3 key plus a
    sha256, so the image is content-addressed and a retry cannot duplicate it.

    One signature per role. Re-signing REPLACES, because the second attempt is the
    one the person meant -- a member who signs, sees a typo in their name and signs
    again should not leave two signatures on the record.
    """

    def post(self, request, order_id):
        import base64
        import hashlib

        from ..models import (
            DispatchOrder, DispatchSignature, DispatchSignerRole,
        )
        from ..services import import_storage

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).select_related("client").first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        role = (request.data.get("role") or "").strip().lower()
        if role not in (DispatchSignerRole.VENDOR, DispatchSignerRole.MEMBER):
            return error("bad_role", "role must be 'vendor' or 'member'.")

        raw_image = request.data.get("image") or ""
        if "," in raw_image:
            raw_image = raw_image.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw_image, validate=True)
        except Exception:  # noqa: BLE001 - any decode failure is the same answer
            return error("bad_image", "image must be a base64 PNG data URL.")
        if not blob:
            return error("bad_image", "The signature is empty.")

        submission = dispatch_svc.open_submission(order)
        digest = hashlib.sha256(blob).hexdigest()
        key = import_storage.build_key(
            f"dispatch-signatures/{order.pk}/{role}-{digest[:16]}.png"
        )
        import_storage.upload_bytes(key, blob, content_type="image/png")

        default_name = (
            request.user.vendor_user.name if role == DispatchSignerRole.VENDOR
            else f"{order.client.first_name} {order.client.last_name}".strip()
        )
        signature, _created = DispatchSignature.objects.update_or_create(
            dispatch_submission=submission, signer_role=role,
            defaults={
                "signer_name": (
                    request.data.get("signer_name") or default_name
                ).strip()[:255],
                "s3_key": key,
                "content_hash": digest,
                "signed_at": timezone.now(),
            },
        )
        return Response({
            "role": signature.signer_role,
            "signer_name": signature.signer_name,
            "signed_at": signature.signed_at,
            "missing_for_submission": dispatch_svc.missing_for_submission(order),
        }, status=http.HTTP_201_CREATED)


class VendorSubmitAssessmentView(VendorAPIView):
    """POST /v1/work/<order_id>/submit/ -- submit the assessment.

    GET reports what is still missing, so the app can disable the button and SAY
    WHY rather than leaving a vendor guessing at the end of a home visit.

    The gate lives in dispatch.missing_for_submission and is enforced HERE as well
    as shown in the app: a UI-only check is how a housing case reached the food
    verification endpoint after the picker had been fixed.
    """

    def get(self, request, order_id):
        from ..models import DispatchOrder

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        return Response({
            "missing": dispatch_svc.missing_for_submission(order),
            "photo_count": order.proofs.count(),
            # Prefilled so a vendor who saved windows, backgrounded the app and came
            # back does not retype them. NOT part of `missing`: they are optional.
            "install_windows": [
                {
                    "date": w.date.isoformat(),
                    "start_time": w.start_time.strftime("%H:%M"),
                    "end_time": w.end_time.strftime("%H:%M"),
                }
                for w in dispatch_svc.install_windows(order)
            ],
        })

    def post(self, request, order_id):
        from django.utils import timezone as tz

        from ..models import (
            DispatchOrder, DispatchQuestionnaire, DispatchQuestionnaireState,
        )
        from ..services.assessment_forms import build_schema

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).select_related("client", "vendor").first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)

        form = DispatchQuestionnaire.objects.filter(dispatch_order=order).first()
        if form is None:
            return error(
                "no_answers", "Save the assessment before submitting it.",
            )
        if form.state == DispatchQuestionnaireState.SUBMITTED:
            # Idempotent: an offline client retrying must not submit twice.
            return Response({
                "state": form.state, "submitted_at": form.submitted_at,
                "already": True,
            })

        missing = dispatch_svc.missing_for_submission(order)
        if missing:
            return error(
                "incomplete",
                "Still needed: " + ", ".join(missing),
                http.HTTP_400_BAD_REQUEST,
                missing=missing,
            )

        # FREEZE the schema. A signed form must render years later exactly as it was
        # signed rather than acquiring blank questions from a later template.
        form.schema_snapshot = build_schema(form.modules or [])
        # ⚠ SAVED BEFORE THE SUBMISSION IS RECORDED, so a bad set of windows is a
        # 400 the vendor can fix rather than a submitted assessment with no install
        # dates and no way back -- the questionnaire becomes read-only at SUBMITTED.
        # Optional: absent means the key was not sent, which is different from an
        # empty list (a deliberate "none") and both are allowed.
        if "install_windows" in (request.data or {}):
            try:
                dispatch_svc.set_install_windows(
                    order, (request.data or {}).get("install_windows") or [],
                )
            except ValueError as exc:
                return error("bad_windows", str(exc))

        form.state = DispatchQuestionnaireState.SUBMITTED
        form.submitted_at = tz.now()
        form.save(update_fields=[
            "schema_snapshot", "state", "submitted_at", "updated_at",
        ])

        # vendor_user, not a bare call: the record has to say WHO submitted, and
        # "the assessor who signed it" is the answer.
        dispatch_svc.submit(order, vendor_user=request.user.vendor_user)

        # The three documents, generated AFTER the submission succeeds and wrapped
        # so a rendering fault cannot undo it. The assessment is the record; the
        # PDFs are derived from it, and losing a submission because a photograph
        # would not decode would be the wrong way round.
        from ..services import dispatch_pdf

        documents = []
        try:
            documents = dispatch_pdf.generate_submission_documents(
                order, vendor_user=request.user.vendor_user,
            )
        except Exception:  # noqa: BLE001
            logger.exception("submission documents failed for order %s", order.pk)

        order.refresh_from_db()
        return Response({
            "state": form.state,
            "submitted_at": form.submitted_at,
            "order_status": order.status,
            "order_status_label": order.get_status_display(),
            "documents": [
                {"doc_type": d.doc_type, "filename": d.filename} for d in documents
            ],
            "already": False,
        }, status=http.HTTP_201_CREATED)


# ── the company's own profile ────────────────────────────────────────────────

def _company_payload(vendor):
    """The company as its administrator sees it.

    Deliberately omits admin_fee_percent and anything else about what we pay them:
    those are OUR commercial terms, editable in the CRM, and a vendor reading their
    own profile has no business seeing the mark-up we add.
    """
    from ..services import import_storage

    logo_url = ""
    if vendor.logo_s3_key:
        try:
            # Presigned on read, never stored. A stored URL expires and leaves a
            # dead link in every PDF generated with it.
            logo_url = import_storage.presign_get(
                vendor.logo_s3_key, expires=3600, inline=True,
            )
        except Exception:  # noqa: BLE001 - a missing logo must not break the page
            logger.warning("vendor logo presign failed: %s", vendor.logo_s3_key)
    return {
        "name": vendor.name,
        "address": vendor.address,
        "contact_phone": vendor.contact_phone,
        "contact_name": vendor.contact_name,
        "contact_email": vendor.contact_email,
        "website": vendor.website,
        "logo_url": logo_url,
        "logo_updated_at": vendor.logo_updated_at,
        "logo_width": vendor.logo_width,
        "logo_height": vendor.logo_height,
    }


class VendorCompanyView(VendorAdminAPIView):
    """GET / PATCH /v1/company/ -- the company's address, phone and contact.

    ADMIN ONLY, via VendorAdminAPIView. A staff user can read their assignments but
    must not be able to change where the company says it is.

    The NAME is read-only here. It is how CareCircle and Unite Us identify the
    company, it appears on authorizations and invoices already issued, and letting a
    vendor rename themselves would silently break the match. Renaming stays a CRM
    action.
    """

    EDITABLE = ("address", "contact_phone", "contact_name", "contact_email", "website")

    def get(self, request):
        return Response(_company_payload(request.user.vendor))

    def patch(self, request):
        vendor = request.user.vendor
        data = request.data or {}
        changed = []
        for field in self.EDITABLE:
            if field not in data:
                continue
            value = (data.get(field) or "").strip()[:255]
            if getattr(vendor, field) != value:
                setattr(vendor, field, value)
                changed.append(field)

        if "name" in data and (data.get("name") or "").strip() != vendor.name:
            # Reported rather than ignored: silently dropping an edit someone typed
            # is how they conclude the save is broken.
            return error(
                "name_read_only",
                "The company name is set by CareCircle. Ask us to change it.",
            )

        if changed:
            vendor.save(update_fields=[*changed, "updated_at"])
            logger.info(
                "vendor company updated: %s fields=%s by=%s",
                vendor.name, changed, request.user.vendor_user.email,
            )
        return Response({**_company_payload(vendor), "changed_fields": changed})


class VendorLogoView(VendorAdminAPIView):
    """POST /v1/company/logo/ -- upload the company logo. DELETE removes it.

    Admin only, same reasoning as the profile.
    """

    parser_classes = [MultiPartParser, FormParser]
    # A ceiling on the UPLOAD, low on purpose -- what gets stored is normalised to
    # about 600px regardless, so a 12 MP original is bytes nobody will ever see.
    MAX_BYTES = 4 * 1024 * 1024
    # Accepted as INPUT. All three are converted to PNG before storage, because
    # that is what embeds reliably in a PDF -- see services/logo_image.py.
    ALLOWED = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

    def post(self, request):
        import hashlib

        from ..services import import_storage

        upload = request.FILES.get("file") or request.FILES.get("logo")
        if upload is None:
            return error("no_file", "Attach an image as 'file'.")
        if upload.size > self.MAX_BYTES:
            return error("too_large", "The logo must be under 4 MB.")
        content_type = (upload.content_type or "").lower()
        if content_type not in self.ALLOWED:
            # Named explicitly: "invalid file" leaves someone guessing whether the
            # problem is the size, the format or the name.
            return error(
                "bad_type", "The logo must be a PNG, JPEG or WebP image.",
            )

        from ..services import logo_image

        vendor = request.user.vendor
        raw = upload.read()

        # NORMALISED BEFORE STORING, not on the way out. A logo is embedded in
        # invoices and quotes, so it is processed once here rather than on every
        # document -- and what is stored is then exactly what prints, which makes
        # "why does it look wrong on the PDF?" answerable by looking at one file.
        try:
            png, info = logo_image.optimise(raw)
        except logo_image.LogoError as exc:
            return error("bad_image", str(exc))

        digest = hashlib.sha256(png).hexdigest()
        # The key names the PROCESSED bytes, so re-uploading the same artwork is
        # idempotent even if it arrived as a JPEG one time and a PNG the next.
        key = import_storage.build_key(f"vendor-logos/{vendor.pk}/{digest[:16]}.png")
        import_storage.upload_bytes(key, png, content_type="image/png")

        vendor.logo_s3_key = key
        vendor.logo_updated_at = timezone.now()
        vendor.logo_width = info["width"]
        vendor.logo_height = info["height"]
        vendor.save(update_fields=[
            "logo_s3_key", "logo_updated_at", "logo_width", "logo_height",
            "updated_at",
        ])
        logger.info(
            "vendor logo uploaded: %s %sx%s by=%s", vendor.name,
            info["width"], info["height"], request.user.vendor_user.email,
        )
        return Response(
            {
                **_company_payload(vendor),
                # Surfaced rather than logged: only the admin can fix a logo that
                # is too small to print well, and they will never read our logs.
                "warning": info["warning"],
            },
            status=http.HTTP_201_CREATED,
        )

    def delete(self, request):
        vendor = request.user.vendor
        if not vendor.logo_s3_key:
            return Response(_company_payload(vendor))
        # The KEY is cleared; the object is left in S3. A logo that appears on
        # already-issued documents must not vanish from them because someone
        # changed the current one.
        vendor.logo_s3_key = ""
        vendor.logo_updated_at = None
        vendor.logo_width = None
        vendor.logo_height = None
        vendor.save(update_fields=[
            "logo_s3_key", "logo_updated_at", "logo_width", "logo_height",
            "updated_at",
        ])
        return Response(_company_payload(vendor))


class VendorWorkOrderStartView(VendorAPIView):
    """POST /v1/work/<order_id>/start/ -- the vendor is on site, starting the job.

    Moves a CONFIRMED work order to Pending Submission and opens the submission that
    will carry the signatures.

    Idempotent: an offline app retrying, or a vendor who backgrounds it and taps
    again, must not open a second submission -- there is no second job.
    """

    def post(self, request, order_id):
        from ..models import DispatchKind, DispatchOrder

        order = DispatchOrder.objects.filter(
            pk=order_id, vendor=request.user.vendor,
        ).select_related("client", "vendor").first()
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        if order.kind != DispatchKind.REMEDIATION:
            return error("wrong_kind", "Only a work order can be started.")
        try:
            dispatch_svc.start_work_order(
                order, vendor_user=request.user.vendor_user,
            )
        except ValueError as exc:
            return error("cannot_start", str(exc))
        order.refresh_from_db()
        return Response({
            "status": order.status,
            "status_label": order.get_status_display(),
            "missing": dispatch_svc.missing_for_completion(order),
        })


class VendorWorkOrderCompleteView(VendorAPIView):
    """GET / POST /v1/work/<order_id>/complete/ -- finish the job.

    GET reports what is still missing so the app can disable the button and SAY WHY,
    rather than leaving a vendor guessing on someone's doorstep. POST checks the same
    gate server-side and renders the two documents.
    """

    def _order(self, request, order_id):
        from ..models import DispatchOrder

        return (
            DispatchOrder.objects
            .filter(pk=order_id, vendor=request.user.vendor)
            .select_related("client", "vendor")
            .prefetch_related("line_items", "proofs")
            .first()
        )

    def get(self, request, order_id):

        order = self._order(request, order_id)
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        photographed = set(
            order.proofs.exclude(dispatch_item=None)
            .values_list("dispatch_item_id", flat=True)
        )
        return Response({
            "status": order.status,
            "missing": dispatch_svc.missing_for_completion(order),
            # Per product, so the app can tick them off rather than print a list of
            # sentences -- the vendor is working through physical devices.
            "items": [
                {
                    "id": str(i.dispatch_item_id),
                    "item": i.item,
                    "location": i.location,
                    "photographed": i.dispatch_item_id in photographed,
                }
                for i in order.line_items.all()
            ],
        })

    def post(self, request, order_id):

        order = self._order(request, order_id)
        if order is None:
            return error("not_found", "No such assignment.", http.HTTP_404_NOT_FOUND)
        from ..models import DispatchKind

        if order.kind != DispatchKind.REMEDIATION:
            return error("wrong_kind", "Only a work order can be completed.")
        try:
            dispatch_svc.complete_work_order(
                order, vendor_user=request.user.vendor_user,
            )
        except ValueError as exc:
            return error("incomplete", str(exc))

        # The documents, AFTER the completion succeeds and wrapped so a rendering
        # fault cannot undo it. The completed job is the record; the PDFs are derived
        # from it, and losing a finished installation because a photograph would not
        # decode would be the wrong way round -- the same order the assessment
        # submission uses.
        documents = []
        try:
            from ..services import dispatch_pdf

            documents = dispatch_pdf.generate_work_order_documents(
                order, vendor_user=request.user.vendor_user,
            )
        except Exception:  # noqa: BLE001
            logger.exception("work order documents failed for order %s", order.pk)

        order.refresh_from_db()
        return Response({
            "status": order.status,
            "status_label": order.get_status_display(),
            "documents": [
                {"doc_type": d.doc_type, "filename": d.filename} for d in documents
            ],
        }, status=http.HTTP_201_CREATED)
