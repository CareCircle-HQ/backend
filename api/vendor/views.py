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

from ..models import (
    DispatchKind, DispatchOrder, DispatchStatus, DispatchVisit, VendorUser,
)
from .auth import (
    IsVendorAdmin, IsVendorUser, VendorAuthentication, authenticate_user,
    client_ip, issue_token,
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
    # Contact details are INHERITED by a work order, like the address: the wizard
    # collects them once on the assessment, so a work order carries none of its
    # own and an installer would otherwise see no number to ring.
    phone, phone_type, notes = order.service_contact
    return {
        "name": f"{first} {last}".strip(),
        "phone": phone,
        "phone_type": phone_type,
        "address": order.service_address,
        "address_notes": notes,
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
        params = request.query_params
        qs = (
            DispatchOrder.objects
            .filter(vendor=request.user.vendor)
            .select_related("client")
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

        supplied = (data.get("password") or "").strip()
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

            supplied = (data.get("password") or "").strip()
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

        windows = list(order.availability_windows.all())
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
