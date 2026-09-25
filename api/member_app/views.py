"""Member mobile-app views.

Every query is scoped from ``request.user`` -- the household member, their household or
their client, depending on what the data belongs to. A ``client_id`` never appears in a
URL on this surface.

⚠ WHAT A MEMBER SEES IS NOT WHAT AN AGENT SEES. The portal's member serializers carry
agent-facing judgements -- hold reasons, warning codes, authorization states, internal
notes -- and a member reading those would be reading what we say about them internally.
Nothing here reuses a portal serializer without stripping it deliberately.
"""
import logging

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    HouseholdMember, HouseholdMemberLoginCode, MemberAccessToken,
)
from .auth import (
    IsMember, MemberAuthentication, authenticate_member, issue_token,
    normalize_username,
)

logger = logging.getLogger(__name__)


def error(code, detail, status_code=http.HTTP_400_BAD_REQUEST, **extra):
    """Machine-readable error body: the app branches on ``code``, not on prose."""
    body = {"error": code, "detail": detail}
    body.update(extra)
    return Response(body, status=status_code)


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", ""))


class MemberAPIView(APIView):
    """Base: member-only auth, and nothing inherited from the CRM's defaults.

    ⚠ IT ALSO ENFORCES THE FORCED PASSWORD CHANGE, the same way the vendor surface does.
    An agent issued the password and read it to the member, so until it is replaced the
    credential is known to two people. Every endpoint returns 403
    ``password_change_required`` until then -- a gate, not a screen the app is trusted
    to show.
    """

    authentication_classes = [MemberAuthentication]
    permission_classes = [IsMember]
    #: True on a view that must work BEFORE the password has been replaced.
    ALLOW_PENDING_PASSWORD = False

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.ALLOW_PENDING_PASSWORD:
            return
        principal = getattr(request, "user", None)
        if getattr(principal, "must_change_password", False):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied({
                "error": "password_change_required",
                "detail": "Choose your own password before continuing.",
            })


class MemberLoginView(APIView):
    """POST /v1/auth/login/ -- mobile number + password.

    One of TWO ways in; the other is the OTP flow the app can offer instead. Both end
    at the same token, so nothing downstream cares which was used.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "anon"

    def post(self, request):
        data = request.data or {}
        username = data.get("username") or data.get("mobile_number") or ""
        password = data.get("password") or ""
        if not username or not password:
            return error(
                "missing_credentials", "Mobile number and password are required.",
            )
        member = authenticate_member(username, password)
        if member is None:
            # ONE message for every failure mode. A different answer for "no such
            # member" would let anyone test which phone numbers belong to Medicaid
            # members -- which is a disclosure in itself.
            return error(
                "invalid_credentials", "Mobile number or password is incorrect.",
                http.HTTP_401_UNAUTHORIZED,
            )
        raw, token = issue_token(
            member,
            ip=_client_ip(request),
            device_label=(data.get("device_label") or "").strip(),
        )
        client = member.client
        return Response({
            "access_token": raw,
            "expires_at": token.expires_at,
            "member": {
                "id": str(member.id),
                "first_name": getattr(client, "first_name", "") if client else "",
                "is_primary": member.is_primary,
                # So the app can go straight to the change screen rather than
                # discovering the 403 by being refused.
                "must_change_password": member.mobile_app_must_change_password,
            },
        })


class MemberLogoutView(MemberAPIView):
    """POST /v1/auth/logout/ -- revoke the presented token.

    Allowed before the forced change: a member who does not want to continue must be
    able to leave.
    """

    ALLOW_PENDING_PASSWORD = True

    def post(self, request):
        MemberAccessToken.objects.filter(pk=request.user.token.pk).update(
            revoked_at=timezone.now(),
        )
        return Response({"ok": True})


class MemberMeView(MemberAPIView):
    """GET /v1/me/ -- who am I.

    Reachable before the password change, or the app cannot render "choose your
    password, NAME".

    ⚠ DELIBERATELY THIN. No Medicaid ID, no plan, no case state, no warnings. This is
    the identity the app needs to address the member, not a profile -- each of those
    belongs to a screen that can justify it.
    """

    ALLOW_PENDING_PASSWORD = True

    def get(self, request):
        member = request.user.household_member
        client = request.user.client
        return Response({
            "member": {
                "id": str(member.id),
                "first_name": getattr(client, "first_name", "") if client else "",
                "last_name": getattr(client, "last_name", "") if client else "",
                "is_primary": member.is_primary,
                "must_change_password": member.mobile_app_must_change_password,
            },
            "household": {"id": str(member.household_id)},
            "token_expires_at": request.user.token.expires_at,
        })


class MemberPasswordView(MemberAPIView):
    """POST /v1/me/password/ -- the member replaces their own password.

    ⚠ ALLOWED WHILE the change is pending, and it has to be: every other endpoint 403s
    until the password changes, and changing it is a call.

    The CURRENT password is required even so. An agent issued it and read it out, so
    somebody else knows it; asking again means a stolen token alone cannot lock the
    real member out of their own benefits.
    """

    ALLOW_PENDING_PASSWORD = True
    MIN_LENGTH = 8

    def post(self, request):
        data = request.data or {}
        current = data.get("current_password") or ""
        # ⚠ NEVER STRIP A PASSWORD ON THE WAY IN. A leading or trailing space is a
        # legitimate character, and trimming it stores something different from what
        # was typed. Trim only to DECIDE whether anything was supplied.
        _raw = data.get("new_password") or ""
        new = _raw if _raw.strip() else ""
        member = request.user.household_member

        if not check_password(current, member.mobile_app_password or ""):
            return error(
                "wrong_password", "That is not your current password.",
                http.HTTP_401_UNAUTHORIZED,
            )
        if len(new) < self.MIN_LENGTH:
            return error(
                "password_too_short",
                f"Choose at least {self.MIN_LENGTH} characters.",
            )
        if check_password(new, member.mobile_app_password or ""):
            return error(
                "password_unchanged", "Choose a password you have not used here.",
            )

        HouseholdMember.objects.filter(pk=member.pk).update(
            mobile_app_password=make_password(new),
            mobile_app_must_change_password=False,
        )
        # ⚠ EVERY OTHER SESSION IS REVOKED. A member changing their password after
        # someone else has seen it expects that to end the other person's access, and
        # the forced first change is exactly that situation.
        MemberAccessToken.objects.filter(
            household_member=member, revoked_at__isnull=True,
        ).exclude(pk=request.user.token.pk).update(revoked_at=timezone.now())
        logger.info("member %s changed their app password", member.id)
        return Response({"ok": True, "must_change_password": False})


class MemberVerifyCodeView(APIView):
    """POST /v1/auth/verify-code/ -- exchange a one-time code for a session.

    THE SECOND WAY IN. A member who has forgotten their password asks for a code
    (``/api/member-app/request-code/`` on the CRM host, which predates this surface)
    and exchanges it here. Both paths end at the same :class:`MemberAccessToken`, so
    nothing downstream cares which was used.

    ⚠ A CODE LOGIN DOES NOT CLEAR ``must_change_password``. A member who never set
    their own password still has an agent-issued one, and letting the OTP route around
    that would turn the forced change into a suggestion.

    ⚠ AND IT DOES NOT SET ONE EITHER. A member who signs in by code with no password at
    all is refused: the CRM's Mobile App tab is the only thing that issues credentials,
    and a self-service path into an unprovisioned account would make the primary-only
    policy unenforceable.
    """

    authentication_classes = []
    permission_classes = []
    throttle_scope = "anon"

    MAX_ATTEMPTS = 5

    def post(self, request):
        from .auth import normalize_username

        data = request.data or {}
        digits = normalize_username(
            data.get("username") or data.get("mobile_number") or "",
        )
        code = str(data.get("code") or "").strip()
        if not digits or not code:
            return error("missing_code", "Mobile number and code are required.")

        # Uniform failure for every reason -- expired, wrong, already used, no such
        # member. A specific answer would tell an attacker which numbers exist and
        # which codes are live.
        generic = error(
            "invalid_code", "That code is not valid. Request a new one.",
            http.HTTP_401_UNAUTHORIZED,
        )

        record = (
            HouseholdMemberLoginCode.objects
            .filter(mobile_number=digits, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if record is None or record.is_expired or record.is_consumed:
            return generic
        if record.attempts >= self.MAX_ATTEMPTS:
            # Burn it rather than leave a guessable code alive.
            HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
                consumed_at=timezone.now(),
            )
            return generic
        if not record.check_code(code):
            HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
                attempts=record.attempts + 1,
            )
            return generic

        member = record.member or (
            HouseholdMember.objects
            .select_related("client", "household")
            .filter(mobile_app_username=digits)
            .first()
        )
        # ⚠ NO PASSWORD MEANS NO ACCOUNT, even with a valid code. 7,235 household
        # members carry a username from an old import; a code login must not become a
        # back door into those.
        if member is None or not member.mobile_app_enabled:
            return generic

        HouseholdMemberLoginCode.objects.filter(pk=record.pk).update(
            consumed_at=timezone.now(), member=member,
        )
        raw, token = issue_token(
            member,
            ip=_client_ip(request),
            device_label=(data.get("device_label") or "").strip(),
        )
        client = member.client
        return Response({
            "access_token": raw,
            "expires_at": token.expires_at,
            "member": {
                "id": str(member.id),
                "first_name": getattr(client, "first_name", "") if client else "",
                "is_primary": member.is_primary,
                "must_change_password": member.mobile_app_must_change_password,
            },
        })


# ── home ─────────────────────────────────────────────────────────────────────

class MemberDashboardView(MemberAPIView):
    """GET /v1/me/dashboard/ -- everything the Home screen shows.

    ONE request, not four. The home screen is the first thing a member sees, often on
    a phone with poor signal, and four round trips means four chances to show a
    spinner. The pieces are small and always shown together.

    ⚠ WHAT IS DELIBERATELY ABSENT: no case state, no hold reason, no warning codes, no
    Medicaid ID, no plan external ids. Those are agent-facing judgements, and a member
    reading them would be reading what we say about them internally.
    """

    def get(self, request):
        from django.utils import timezone

        from ..models import DeliveryOrder, Insurance

        member = request.user.household_member
        client = request.user.client
        today = timezone.localdate()

        first_name = (getattr(client, "first_name", "") or "").strip()

        # ── coverage ──────────────────────────────────────────────────────
        coverage = {"status": "unknown", "plan_name": "", "valid_until": None}
        if client is not None:
            ins = (
                Insurance.objects
                .filter(client=client)
                .order_by("-is_primary", "-enrolled_at")
                .first()
            )
            if ins is not None:
                coverage["plan_name"] = (ins.plan_name or ins.plan_type or "").strip()
                coverage["status"] = (
                    "active" if (ins.status or "").lower() == "active"
                    else "inactive"
                )
                # ⚠ 9999-12-31 IS A SENTINEL, NOT A DATE, and it is 47% of all
                # insurance rows. "Valid until: Dec 31, 9999" is nonsense on a
                # member's phone; an open-ended coverage has no end date to show, so
                # this returns null and the screen says "ongoing" instead.
                if ins.expired_at and ins.expired_at.year < 9999:
                    coverage["valid_until"] = ins.expired_at.date()

        # ── the next delivery ─────────────────────────────────────────────
        # ⚠ SCOPED TO THE CLIENT, not the household. DeliveryOrder.member is a
        # Client, and meals are cooked to ONE person's dietary profile -- showing a
        # household's deliveries would show a member food they must not eat.
        next_delivery = None
        if client is not None:
            row = (
                DeliveryOrder.objects
                .filter(member=client, expected_delivery_date__gte=today)
                .exclude(status__in=["cancelled", "canceled"])
                .order_by("expected_delivery_date")
                .first()
            )
            if row is not None:
                next_delivery = {
                    "date": row.expected_delivery_date,
                    "quantity": row.quantity,
                    "status": row.status,
                    "is_today": row.expected_delivery_date == today,
                }

        return Response({
            "member": {
                "id": str(member.id),
                # Title-cased: the CRM stores "JAMES BETHEA" in caps for matching, and
                # "Good morning, JAMES" reads as shouting at the person.
                "first_name": first_name.title(),
            },
            "coverage": coverage,
            "next_delivery": next_delivery,
        })


# ── benefits ─────────────────────────────────────────────────────────────────

#: What a member calls the thing, keyed by our internal service_type.
#:
#: ⚠ SERVICE TYPES ARE PAYER VOCABULARY. "Home Expense Assistance/Repairs" and
#: "Environmental Exposure Assessment" are what the 1115 waiver calls these; nobody
#: describes their own home that way. An unmapped type falls back to the raw value
#: rather than being hidden -- a benefit missing from this screen is worse than one
#: with an awkward name.
BENEFIT_LABELS = {
    "Medically Tailored Meals": "Medically Tailored Meals",
    "Produce Prescription/Voucher": "Fresh Produce",
    "Home Expense Assistance/Repairs": "Home Repairs",
    "Environmental Exposure Assessment": "Dwelling Assessment",
    "Environmental Modifications/Accessibility": "Home Modifications",
    "Food Pantry": "Food Pantry",
}

BENEFIT_BLURBS = {
    "Medically Tailored Meals":
        "Meals designed by a dietitian for your health conditions, delivered to "
        "your home at no cost.",
    "Produce Prescription/Voucher":
        "Fresh fruit and vegetables, at no cost to you.",
    "Home Expense Assistance/Repairs":
        "Equipment and repairs that make your home healthier — such as an air "
        "conditioner, heater or dehumidifier.",
    "Environmental Exposure Assessment":
        "A specialist visits your home to find anything affecting your health.",
    "Environmental Modifications/Accessibility":
        "Changes to your home that make it safer and easier to move around.",
    "Food Pantry": "Groceries from a local pantry.",
}

#: ⚠ NOT BENEFITS, AND THEY MUST NOT APPEAR HERE. ``eligibility`` and ``navigation``
#: cases are OUR process -- screening a member and managing their file. A member
#: reading "Social Service Case Management" on a list of their benefits would think
#: they had been given something they have not.
BENEFIT_CASE_TYPES = {"internal_service"}


#: How a member should hear a dispatch status. The internal vocabulary is workflow
#: state -- "Pending Submission" means the vendor has not filed their paperwork, which
#: is true and is none of the member's business.
#:
#: ⚠ AND THIS IS THE STATUS THAT MATTERS FOR HOUSING. The CASE's status describes a
#: funding authorization; the DISPATCH ORDER describes whether somebody is coming to
#: the member's home. "Expired" on a Home Assessment card meant the authorization
#: window had lapsed -- while an assessor was booked for next Monday.
DISPATCH_MEMBER_STATUS = {
    "pending_schedule": ("pending", "We are arranging a visit."),
    "confirmed": ("active", "A visit is booked."),
    "pending_submission": ("active", "The visit has happened. We are finishing up."),
    "submitted": ("active", "The visit is complete."),
    "uploaded": ("active", "The visit is complete."),
    "cancelled": ("expired", "This visit was cancelled."),
}

#: ⚠ THE DWELLING ASSESSMENT IS OPEN OR CLOSED, NOTHING ELSE. Its own case status is
#: the whole truth: either the assessment is still running or it is finished. The
#: dispatch workflow underneath ("Pending Submission", "Uploaded") describes the
#: VENDOR's paperwork, and exposing six workflow states for a two-state thing invited
#: exactly the bug it caused -- "The visit is complete" printed about an appointment
#: three days away.
ASSESSMENT_CASE_STATUS = {
    "open": ("active", "Open"),
    "closed": ("expired", "Closed"),
}

#: Which dispatch kind speaks for which benefit.
SERVICE_DISPATCH_KIND = {
    "Environmental Exposure Assessment": "assessment",
    "Home Expense Assistance/Repairs": "remediation",
    "Environmental Modifications/Accessibility": "remediation",
}


class MemberBenefitsView(MemberAPIView):
    """GET /v1/me/benefits/ -- what this member is actually receiving.

    ⚠ ONE CARD PER SERVICE, NOT ONE PER CASE. A case is a funding vehicle: James
    Bethea has 22 cases, of which NINE are Home Remediation for five different devices
    in open/closed pairs. Listed raw that is nine near-identical cards and no way to
    tell what he has. Grouped by service it is one card that says which devices.

    Status is derived from the group, strongest first -- a member with one live meals
    case and three closed ones has meals, and saying "expired" because most rows are
    closed would be a lie with real consequences.
    """

    def get(self, request):
        from django.utils import timezone

        from ..models import Case, DispatchOrder

        client = request.user.client
        if client is None:
            return Response({"benefits": []})

        today = timezone.now()
        groups = {}
        for case in Case.objects.filter(
            client=client, case_type__in=BENEFIT_CASE_TYPES,
        ).order_by("-case_created_at"):
            key = case.service_type or "Other"
            g = groups.setdefault(key, {"cases": [], "items": []})
            g["cases"].append(case)
            # ⚠ ONLY FOR HOME REMEDIATION. Its programme names carry the DEVICE,
            # which is the only thing distinguishing nine otherwise identical cases:
            #   "Home Remediation - Air Conditioner - Queens" -> "Air Conditioner"
            #
            # Applied to every service this produced noise that means nothing to a
            # member: "Medically Tailored Meals (MTM) - Other Eligible Populations"
            # yielded "Other Eligible Populations", which is a PAYER POPULATION
            # CATEGORY, not something anybody has been given. Caught by reading the
            # real output rather than by a test.
            name = (case.program_name or "").strip()
            if name.startswith("Home Remediation - ") and " - " in name:
                device = name.split(" - ")[1].strip()
                if device and device not in g["items"]:
                    g["items"].append(device)

        out = []
        for service_type, g in groups.items():
            status = "expired"
            valid_until = None
            note = ""
            for case in g["cases"]:
                auth = (case.service_authorization_status or "").lower()
                start, end = case.effective_authorization_window()
                live = (
                    case.case_status == "open"
                    and auth == "approved"
                    and (end is None or end >= today)
                )
                if live:
                    status = "active"
                    # The furthest-out end date in the group: it is the answer to
                    # "how long have I got this for".
                    if end is not None and end.year < 9999:
                        d = end.date()
                        valid_until = d if valid_until is None else max(valid_until, d)
                    break
                if auth in {"pending", "in_review"} and status != "active":
                    status = "pending"

            # ⚠ A HOUSING BENEFIT'S STATUS IS ITS VISIT, NOT ITS AUTHORIZATION. For
            # an assessment or a repair the member's question is "is somebody
            # coming?", and the dispatch order answers it; the case only says whether
            # a payer has agreed to fund it. James Bethea's Home Assessment card read
            # "expired" while an assessor was booked for the following Monday.
            visit_on = None
            if service_type == "Environmental Exposure Assessment":
                # ⚠ OPEN OR CLOSED, from the case. An OPEN case wins over a closed one:
                # James Bethea has both, and "Closed" on a member's screen while their
                # assessment is still running would tell them it is over.
                open_case = any(c.case_status == "open" for c in g["cases"])
                status, note = ASSESSMENT_CASE_STATUS[
                    "open" if open_case else "closed"
                ]
                out.append({
                    "id": service_type,
                    "name": BENEFIT_LABELS.get(service_type, service_type),
                    "description": BENEFIT_BLURBS.get(service_type, ""),
                    "status": status,
                    "note": note,
                    "visit_on": None,
                    "valid_until": None,
                    "items": [],
                    "case_count": len(g["cases"]),
                })
                continue

            kind = SERVICE_DISPATCH_KIND.get(service_type)
            if kind:
                order = (
                    DispatchOrder.objects
                    .filter(client=client, kind=kind)
                    .exclude(status="cancelled")
                    .order_by("-created_at")
                    .first()
                )
                if order is not None:
                    mapped = DISPATCH_MEMBER_STATUS.get(order.status)
                    if mapped:
                        status, note = mapped
                    visit = (
                        order.visits
                        .filter(scheduled_for__isnull=False)
                        .order_by("-scheduled_for")
                        .first()
                    )
                    if visit is not None:
                        visit_on = visit.scheduled_for
                        # ⚠ A FUTURE VISIT OVERRIDES THE PAPERWORK STATUS. The order
                        # status is the vendor's workflow, and it can say "submitted"
                        # while the appointment is still days away -- James Bethea's
                        # assessment is submitted with a visit on the 28th, read on
                        # the 25th. Telling a member "the visit is complete" about an
                        # appointment they have not had yet is worse than saying
                        # nothing, and they may not open the door for it.
                        if visit_on > timezone.now():
                            status, note = "active", "A visit is booked."

            out.append({
                "id": service_type,
                "name": BENEFIT_LABELS.get(service_type, service_type),
                "description": BENEFIT_BLURBS.get(service_type, ""),
                "status": status,
                # Said in member language when we have a visit to speak about.
                "note": note,
                "visit_on": visit_on,
                "valid_until": valid_until,
                # Named devices for housing; empty for meals, where the service IS
                # the thing.
                "items": g["items"][:6],
                "case_count": len(g["cases"]),
            })

        # Active first, then pending, then expired -- a member opens this to see what
        # they HAVE. Alphabetical within a status so the order does not jump around
        # between loads.
        rank = {"active": 0, "pending": 1, "expired": 2}
        out.sort(key=lambda b: (rank.get(b["status"], 3), b["name"]))
        return Response({"benefits": out})


class MemberDeliveriesView(MemberAPIView):
    """GET /v1/me/deliveries/ -- the food programme detail screen.

    Returns the NEXT delivery, the LAST completed one, and a short history.

    ⚠ "LAST DELIVERY" IS EMPTY FOR ALMOST EVERYBODY. Of 472,628 delivery orders only
    184 are marked delivered -- 0.04% -- and just 184 of 19,418 members with any
    delivery have one. 252,152 are CANCELLED and 220,292 sit at ready_for_delivery.
    Whether that reflects reality or a status feed that never closes the loop is a
    question for the delivery pipeline, not for this screen; what this screen must do
    is not pretend. It returns null and the app says so.

    ⚠ THERE IS NO DELIVERY TIME WINDOW IN THE DATA. The mock showed "10:00 AM -
    2:00 PM" against every delivery; no field carries it. Inventing one would have a
    member waiting in for a window nobody promised.
    """

    #: A couple of months of context. A member scrolling years of meal deliveries is
    #: not a use case anybody has; the next one and the recent past is.
    HISTORY_LIMIT = 20

    def get(self, request):
        from django.utils import timezone

        from ..models import DeliveryOrder

        client = request.user.client
        if client is None:
            return Response({"next": None, "last": None, "history": []})

        today = timezone.localdate()
        # ⚠ SCOPED TO THE CLIENT, not the household: meals are cooked to ONE person's
        # dietary profile, and another member's allergen-free meals are not theirs.
        # Cancelled excluded HERE, not per-query: `total` is derived from this, and
        # reporting 34 while the list shows 15 is the kind of mismatch a member reads
        # as missing deliveries.
        base = (
            DeliveryOrder.objects
            .filter(member=client)
            .exclude(status__in=["cancelled", "canceled"])
            .select_related("menu_type")
        )

        def block(row):
            if row is None:
                return None
            return {
                "id": str(row.delivery_order_id),
                "date": row.expected_delivery_date,
                "delivered_at": row.delivered_at,
                "quantity": row.quantity,
                "status": row.status,
                # What KIND of meal, in the member's terms. Two fields because the
                # kitchen and the menu describe different things -- "Dairy Free" is
                # the menu, "Allergen Free" is how the kitchen prepares it.
                "menu_type": str(row.menu_type) if row.menu_type_id else "",
                "meal_type": (row.kitchen_meal_type or "").strip(),
                "is_today": row.expected_delivery_date == today,
                # Whether anyone actually confirmed the food arrived. False on almost
                # every row -- see the note on `last`.
                "confirmed": row.delivered_at is not None,
            }

        # ⚠ AND A DELIVERY THAT ALREADY ARRIVED IS NOT "NEXT". Today's order stays in
        # range until midnight, so once it is confirmed it was being shown as the
        # thing still to come -- "Next Delivery: today" to someone holding the food.
        nxt = (
            base.filter(expected_delivery_date__gte=today, delivered_at__isnull=True)
            .order_by("expected_delivery_date")
            .first()
        )
        # ⚠ THE MOST RECENT PAST DELIVERY, NOT THE MOST RECENT *CONFIRMED* ONE.
        #
        # Requiring delivered_at was technically correct and practically useless:
        # James Bethea has THIRTY past deliveries and not one carries it, so the
        # screen told him "no delivery has been confirmed yet" — which reads as "you
        # have never been sent food". Across the whole system only 184 of 472,628
        # orders are marked delivered, so that was the answer for 99% of members.
        #
        # What we actually know is: an order was scheduled for a past date, and
        # whether anyone confirmed it. Both are worth showing; conflating them is not.
        # `confirmed` carries the distinction and the app words it honestly rather
        # than claiming an arrival we cannot evidence.
        # ⚠ "PAST" IS date < today OR delivered_at SET. Excluding today's date left a
        # delivery that ARRIVED THIS MORNING showing as neither next nor last -- it
        # had dropped out of "next" once confirmed, and never entered "last" until
        # midnight. The member's most recent delivery simply vanished for a day.
        from django.db.models import Q

        last = (
            base.filter(
                Q(expected_delivery_date__lt=today) | Q(delivered_at__isnull=False),
            )
            .order_by("-expected_delivery_date", "-delivered_at")
            .first()
        )
        # ⚠ CANCELLED DELIVERIES ARE EXCLUDED HERE TOO -- see the note on the history
        # endpoint. 99% are same-day re-planning, so listing them reports failures
        # that did not happen.
        history = [
            block(r) for r in base
            .filter(expected_delivery_date__lt=today)
            .order_by("-expected_delivery_date")[:self.HISTORY_LIMIT]
        ]

        return Response({
            "next": block(nxt),
            "last": block(last),
            "history": history,
            # So the screen can say "no deliveries yet" rather than "none this month".
            "total": base.count(),
        })


class MemberDeliveryHistoryView(MemberAPIView):
    """GET /v1/me/deliveries/history/?limit=&offset= -- every delivery, newest first.

    Separate from ``/me/deliveries/`` because it answers a different question. That
    one is "what is happening with my food"; this is "what have I been sent", which a
    member opens to check a specific week or to count what arrived.

    ⚠ PAGINATED even though the heaviest member has only 292 deliveries. It is a
    phone, often on cellular, and a member who wants the last month should not wait
    for six years of history to arrive first.
    """

    DEFAULT_LIMIT = 50
    MAX_LIMIT = 200

    def get(self, request):
        from ..models import DeliveryOrder

        client = request.user.client
        if client is None:
            return Response({
                "deliveries": [], "total": 0, "has_more": False, "summary": {},
            })

        try:
            limit = min(int(request.query_params.get("limit", self.DEFAULT_LIMIT)),
                        self.MAX_LIMIT)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            limit, offset = self.DEFAULT_LIMIT, 0

        # ⚠ CANCELLED ORDERS ARE HIDDEN, and the data is why. 99% of cancelled
        # (member, date) pairs ALSO have a live order on that same date -- 37,608 of
        # 38,052 sampled -- and for James Bethea it is 12 of 12. A cancellation here
        # is almost always RE-PLANNING: an order is voided and immediately reissued,
        # usually for a changed quantity or kitchen.
        #
        # So showing them told a member "your food was cancelled 12 times" on twelve
        # days they actually received food. I argued earlier for keeping them --
        # "someone whose food did not come is exactly who opens this screen" -- and
        # that was reasoning about a delivery system this is not. The 1% where a
        # cancellation stands alone is a real gap, but it is a support conversation,
        # not a row a member should have to interpret.
        base = (
            DeliveryOrder.objects
            .filter(member=client)
            .exclude(status__in=["cancelled", "canceled"])
            .select_related("menu_type")
            # ⚠ SECOND KEY ON THE ID. A member can still have two orders on one date
            # without a tiebreak their order changes between pages, so a row can
            # appear twice or not at all while scrolling.
            .order_by("-expected_delivery_date", "-created_at")
        )
        total = base.count()
        rows = base[offset:offset + limit]

        # Counted over EVERYTHING, not the page: "14 delivered" must not change as
        # the member scrolls.
        from django.db.models import Count

        # ⚠ .order_by() FIRST, TO CLEAR THE ORDERING. values().annotate() adds every
        # ORDER BY field to the GROUP BY, so grouping a queryset ordered by
        # (-expected_delivery_date, -created_at) counts one group PER ROW. The dict
        # comprehension then kept the last group per status and reported
        # "cancelled: 1" for a member with sixteen cancellations.
        counts = {
            r["status"]: r["n"] for r in
            base.order_by().values("status").annotate(n=Count("delivery_order_id"))
        }

        return Response({
            "deliveries": [
                {
                    "id": str(r.delivery_order_id),
                    "date": r.expected_delivery_date,
                    "delivered_at": r.delivered_at,
                    "quantity": r.quantity,
                    "status": r.status,
                    "confirmed": r.delivered_at is not None,
                    "menu_type": str(r.menu_type) if r.menu_type_id else "",
                    "meal_type": (r.kitchen_meal_type or "").strip(),
                }
                for r in rows
            ],
            "total": total,
            "has_more": offset + limit < total,
            "summary": {
                "delivered": counts.get("delivered", 0),
                # No cancelled count: they are excluded above, so it would always be
                # zero -- and a running tally of "times we failed to send your food"
                # is not a summary a member wants at the top of the screen anyway.
                "scheduled": sum(
                    n for s, n in counts.items()
                    if s not in {"delivered", "cancelled", "canceled"}
                ),
            },
        })


class MemberDeliveryPhotosView(MemberAPIView):
    """GET /v1/me/deliveries/<delivery_id>/photos/ -- proof that food arrived.

    ⚠ SHORT-LIVED SIGNED URLS, NEVER A PUBLIC ONE. A proof-of-delivery photograph
    shows a member's front door, and often their street number and sometimes them.
    A public S3 URL is forever and is guessable across members; a 15-minute presigned
    link expires with the screen. This is what D4 asked and it is the same mechanism
    the vendor documents already use.

    ⚠ AND THE ORDER IS RE-SCOPED TO THE MEMBER, not merely looked up. The delivery id
    comes from the client, so fetching it without the member filter would let any
    signed-in member read any other member's doorstep photographs by changing a uuid.

    ⚠ THERE ARE CURRENTLY NO PROOFS AT ALL -- DeliveryOrderProof holds ZERO rows
    against 472,628 delivery orders, and nothing has ever written one despite
    ``services.pod_ingest`` being wired to both the CSV importer and the partner API.
    So this endpoint is correct and will return an empty list for every member until
    a delivery company actually sends one. That is a pipeline question, not a reason
    to leave the screen unbuilt.
    """

    PRESIGN_SECONDS = 900

    def get(self, request, delivery_id):
        from ..models import DeliveryOrder
        from ..services import import_storage

        client = request.user.client
        order = (
            DeliveryOrder.objects
            .filter(delivery_order_id=delivery_id, member=client)
            .select_related("menu_type")
            .first()
            if client is not None else None
        )
        if order is None:
            # 404 rather than 403: whether a delivery id exists is not something a
            # member needs told about somebody else's order.
            return error("not_found", "No such delivery.", http.HTTP_404_NOT_FOUND)

        photos = []
        for proof in order.proofs.all().order_by("created_at"):
            url = ""
            if proof.s3_key:
                try:
                    url = import_storage.presign_get(
                        proof.s3_key, expires=self.PRESIGN_SECONDS, inline=True,
                        download_name=f"delivery-{order.expected_delivery_date}.jpg",
                        content_type=proof.content_type or "image/jpeg",
                    )
                except Exception:  # noqa: BLE001 - one bad key must not lose the rest
                    logger.warning(
                        "member photos: could not presign %s", proof.s3_key,
                    )
            if not url:
                continue
            photos.append({
                "id": str(proof.id),
                "url": url,
                # Named so the app can say "expired, pull to refresh" rather than
                # showing a broken image when a member leaves the screen open.
                "expires_in": self.PRESIGN_SECONDS,
                "taken_at": proof.delivered_at or proof.created_at,
                "note": (proof.note or "").strip(),
            })

        return Response({
            "delivery": {
                "id": str(order.delivery_order_id),
                "date": order.expected_delivery_date,
                "delivered_at": order.delivered_at,
                "quantity": order.quantity,
                "status": order.status,
                "confirmed": order.delivered_at is not None,
                # For the detail screen: the same descriptors the list shows, so
                # opening a row does not lose information the member could already see.
                "menu_type": str(order.menu_type) if order.menu_type_id else "",
                "meal_type": (order.kitchen_meal_type or "").strip(),
                "note": (order.delivery_note or "").strip(),
            },
            "photos": photos,
        })


class MemberAssessmentView(MemberAPIView):
    """GET /v1/me/assessment/ -- what the assessor recommended, and where each item is.

    Opened from the Dwelling Assessment card. The member's question after a visit is
    "what did they say I need, and is it coming?", and until now nothing answered it.

    ⚠ NO PRICES, EVER. The vendor's rates, the programme cap and the authorized amount
    are all on these records and none of them belongs on a member's phone: the money is
    between CareCircle, the vendor and the payer. The member needs the ITEM and its
    STATE.

    ⚠ AND NO DENIAL REASONS. `service_authorization_denial_reason` is payer language
    written for an appeal, and a member reading "not medically necessary" about their
    own home on a phone screen, with nobody to ask, is a cruelty. It says the item was
    not approved and to contact CareCircle, which is the action that can actually help.
    """

    def get(self, request):
        from ..models import DispatchItem, DispatchOrder
        from ..services import dispatch as dispatch_svc

        client = request.user.client
        if client is None:
            return Response({"assessment": None, "items": []})

        order = (
            DispatchOrder.objects
            .filter(client=client, kind="assessment")
            .exclude(status="cancelled")
            .order_by("-created_at")
            .first()
        )
        if order is None:
            return Response({"assessment": None, "items": []})

        visit = (
            order.visits.filter(scheduled_for__isnull=False)
            .order_by("-scheduled_for").first()
        )
        questionnaire = getattr(order, "questionnaire", None)
        recommended = dispatch_svc.recommended_products(order)

        # ⚠ ONE ROW PER PRODUCT, not per DispatchItem. James Bethea has TEN items for
        # five products -- an open case and a closed one for each -- and listing them
        # raw shows a member "Heater" twice with different answers. Grouped, the
        # strongest state wins, because "one of your heaters is on its way" is the true
        # and useful reading.
        by_product = {}
        for item in DispatchItem.objects.filter(
            assessment=order,
        ).select_related("dispatch_order", "case"):
            g = by_product.setdefault(item.item, [])
            g.append(item)

        # ⚠ ONE STATE ONLY: IDENTIFIED. An assessment IDENTIFIES what a home needs
        # -- that is its entire output. Whether each item is then approved, booked or
        # fitted belongs to the Home Repairs benefit, which tracks the cases doing it.
        #
        # My first version derived five states here from the work orders, which made
        # the assessment screen a second, competing view of the repairs and produced
        # "Installed." about devices nobody had fitted yet. Fewer states, in the right
        # place.
        items = []
        for product, group in sorted(by_product.items()):
            items.append({
                "item": product,
                # The assessor's recommended quantity where we have it; one otherwise.
                "quantity": recommended.get(product) or 1,
                "status": "identified",
                "note": "Identified by the assessor as needed for your home.",
            })

        # Products the assessor recommended that have no case at all yet -- otherwise a
        # member sees four of the five things they were told about.
        #
        # ⚠ COMPARED ON product_key, NOT THE RAW NAME. billing_category says "Air
        # Filtration DeviceS" while the case is "Air Filtration Device", so an exact
        # comparison listed that product TWICE -- once from the case and once from the
        # recommendation, spelled differently, with different statuses. The same
        # mismatch was silently hiding the product in the CRM's work-order picker.
        seen_keys = {dispatch_svc.product_key(p) for p in by_product}
        for product, qty in recommended.items():
            if dispatch_svc.product_key(product) not in seen_keys:
                items.append({
                    "item": product, "quantity": qty, "status": "identified",
                    "note": "Identified by the assessor as needed for your home.",
                })

        return Response({
            "assessment": {
                "id": str(order.pk),
                "status": order.status,
                "visit_on": visit.scheduled_for if visit else None,
                "completed": bool(
                    questionnaire and questionnaire.state == "submitted",
                ),
                # ⚠ SAID PLAINLY when there is no recommendation recorded. An
                # assessment taken on paper has no questionnaire, and an empty list
                # would read as "the assessor found nothing wrong".
                "has_recommendation": bool(recommended or by_product),
            },
            "items": items,
        })


#: A Home Repairs product's state, strongest first. Four words, and each is a thing
#: that happened rather than a workflow stage.
REPAIR_STATUS_LABELS = {
    "installed": "Installed",
    "booked": "Booked",
    "approved": "Approved",
    "denied": "Denied",
}


class MemberRepairsView(MemberAPIView):
    """GET /v1/me/repairs/ -- the Home Repairs cases, and proof of what was fitted.

    ⚠ APPROVED CASES ONLY. James Bethea has ten repair cases: five approved and open,
    five DENIED and closed -- the denials being earlier attempts at the same five
    products. Listing all ten shows every product twice, once as a refusal, and a
    member cannot tell which one is live. A denial that was later approved is not news;
    it is our history with the payer.

    ⚠ INSTALLED MEANS THE WORK ORDER WAS SUBMITTED. Not "the case is closed" -- a
    housing case can close in Unite Us while the device still has to be fitted, which
    is documented on the model itself. The vendor filing their completed work order is
    the event that means a member has the thing.

    The installation photographs come from the vendor app: one per product, captured
    on site, behind the same short-lived signed URLs as the delivery proofs.
    """

    PRESIGN_SECONDS = 900

    def get(self, request):
        from ..models import Case, DispatchProof
        from ..services import import_storage

        client = request.user.client
        if client is None:
            return Response({"items": []})

        cases = (
            Case.objects
            .filter(
                client=client,
                case_type="internal_service",
                service_type="Home Expense Assistance/Repairs",
                service_authorization_status="approved",
            )
            .prefetch_related("dispatch_items__dispatch_order")
            .order_by("program_name")
        )

        items = []
        for case in cases:
            name = (case.program_name or "").strip()
            product = (
                name.split(" - ")[1].strip()
                if name.startswith("Home Remediation - ") and " - " in name
                else (case.service_type or "Home repair")
            )

            state = "approved"
            work_order = None
            for item in case.dispatch_items.all():
                wo = item.dispatch_order
                if wo is None:
                    continue
                work_order = wo
                if wo.status in {"submitted", "uploaded"}:
                    state = "installed"
                    break
                if wo.status != "cancelled":
                    state = "booked"

            # Photos of THIS product, taken by the vendor on site. Keyed through the
            # dispatch item, which is why that FK exists.
            photos = []
            item_ids = [i.dispatch_item_id for i in case.dispatch_items.all()]
            if item_ids:
                for proof in (
                    DispatchProof.objects
                    .filter(dispatch_item_id__in=item_ids)
                    .order_by("captured_at")
                ):
                    if not proof.s3_key:
                        continue
                    try:
                        url = import_storage.presign_get(
                            proof.s3_key, expires=self.PRESIGN_SECONDS, inline=True,
                            download_name=f"{product}.jpg",
                        )
                    except Exception:  # noqa: BLE001 - one bad key loses one photo
                        logger.warning(
                            "member repairs: could not presign %s", proof.s3_key,
                        )
                        continue
                    photos.append({
                        "id": str(proof.id),
                        "url": url,
                        "taken_at": proof.captured_at,
                        "note": (proof.caption or "").strip(),
                    })

            items.append({
                "id": str(case.case_id),
                "item": product,
                "status": state,
                "status_label": REPAIR_STATUS_LABELS[state],
                "visit_on": None,
                "photos": photos,
            })

            if work_order is not None:
                visit = (
                    work_order.visits.filter(scheduled_for__isnull=False)
                    .order_by("-scheduled_for").first()
                )
                if visit is not None:
                    items[-1]["visit_on"] = visit.scheduled_for

        return Response({"items": items})
