"""Build/refresh the EnrollmentAnalytics read model (Administration > Data page).

One row per MEMBER (Client) -- every member, including those with no enrollment /
no internal-service case -- flattening every Data-page filter field for the
member's active/governing enrollment (incl. derived delivery status + multi-valued
dietary/eligibility arrays). Rebuilt on a schedule (~hourly) -- see
tasks.rebuild_enrollment_analytics and docs/analytics-architecture.md. Not the
source of truth.
"""

import logging

from django.db.models import Prefetch
from django.utils import timezone

from ..models import (
    Assessment, Case, Client, DeliveryOrder, EnrollmentAnalytics,
    EnrollmentVerification, Insurance, MemberDietaryProfile, Screening,
    SocialCareCoverage,
)

logger = logging.getLogger(__name__)

_INTERNAL_SERVICE = "internal_service"


def _cadence_from_weekdays(weekdays):
    """Normalize delivery weekdays -> a DeliveryCadence-style code for filtering."""
    s = {(w or "").strip().lower()[:3] for w in (weekdays or []) if w}
    if s == {"mon", "thu"}:
        return "mon_thu"
    if s == {"tue", "fri"}:
        return "tue_fri"
    if len(s) == 1:
        return "once_a_week"
    return ""


def _clean(seq):
    """De-duped, order-preserving list of non-empty strings for an array column."""
    out, seen = [], set()
    for v in seq or []:
        v = (str(v) if v is not None else "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _derive_delivery(client_id):
    """(current_delivery_status, last_po_delivery_status, last_delivered_at) for a
    member, from their delivery orders (latest by expected date)."""
    orders = list(
        DeliveryOrder.objects.filter(member_id=client_id)
        .select_related("purchase_order", "delivery_company")
        .order_by("-expected_delivery_date", "-created_at")[:50]
    )
    if not orders:
        return "", "", None, ""
    latest = orders[0]
    current = latest.status or ""
    last_po = (latest.purchase_order.delivery_status
               if latest.purchase_order else "") or ""
    delivered = next(
        (o.delivered_at or o.expected_delivery_date
         for o in orders if o.status == "delivered"), None
    )
    company = next(
        (o.delivery_company.name for o in orders if o.delivery_company_id), ""
    )
    return current, last_po, _as_aware(delivered), company


def _in_any_po(client_id):
    """True when the member has ever been included in a generated Purchase Order
    (a DeliveryOrder line tied to a PO), regardless of delivery status. POs carry
    a DeliveryOrder line PER MEMBER (dependents included -- confirmed in the
    data: nearly every member of a delivered household has their own line), so
    this is a per-member check."""
    return DeliveryOrder.objects.filter(
        member_id=client_id, purchase_order__isnull=False
    ).exists()


def _has_active_delivery(client_id):
    """True when the member has an ACTIVE delivery calendar -- a non-cancelled
    DeliveryOrder tied to a PO (a real scheduled/live/delivered order). Distinct
    from ``_in_any_po`` (EVER in a PO, incl. all-cancelled): this is the "being
    delivered right now" signal used by the Active company status."""
    return DeliveryOrder.objects.filter(
        member_id=client_id, purchase_order__isnull=False,
    ).exclude(status="cancelled").exists()


def _as_aware(value):
    """Coerce a date/naive-datetime into an aware datetime (expected_delivery_date
    is a DateField), so storing it in a DateTimeField doesn't warn or shift days."""
    if value is None:
        return None
    import datetime as _dt
    if isinstance(value, _dt.datetime):
        return timezone.make_aware(value) if timezone.is_naive(value) else value
    if isinstance(value, _dt.date):
        return timezone.make_aware(_dt.datetime(value.year, value.month, value.day))
    return value


def _coverage(client_id):
    ins = (Insurance.objects.filter(client_id=client_id)
           .order_by("-is_primary", "-enrolled_at").first())
    soc = (SocialCareCoverage.objects.filter(client_id=client_id)
           .order_by("-enrolled_at").first())
    return (
        (ins.status if ins else ""), (ins.expired_at if ins else None),
        (soc.status if soc else ""), (soc.expired_at if soc else None),
    )


def _dietary(enrollment_id, client_id):
    prof = (MemberDietaryProfile.objects
            .filter(enrollment_id=enrollment_id, client_id=client_id).first())
    if prof is None:
        return "", [], [], [], ""
    return (
        prof.menu_type or "",
        _clean(prof.food_allergies), _clean(prof.conditions), _clean(prof.medications),
        prof.status or "",
    )


def _screening_assessment(client_id):
    scr = (Screening.objects.filter(client_id=client_id)
           .order_by("-screen_created_at").first())
    asm = (Assessment.objects.filter(client_id=client_id)
           .order_by("-screen_created_at").first())
    eligible = _clean(asm.eligible_services) if asm else []
    return (
        scr is not None, (scr.screen_created_at if scr else None),
        asm is not None, (asm.screen_created_at if asm else None),
        eligible,
    )


def _parity_fields(client):
    """Fields mirrored EXACTLY from the Members list (via MemberListSerializer +
    the same view helpers), so the Data page numbers match the Members page.
    Best-effort per field: never let one lookup fail the whole row."""
    from api.models import Ticket, TicketStatus
    from api.portal.serializers import MemberListSerializer

    try:
        row = MemberListSerializer(client).data
    except Exception:  # noqa: BLE001
        row = {}

    try:
        from api.portal.views_members import MembersListView
        service_type = MembersListView._service_type_for_client(client) or ""
    except Exception:  # noqa: BLE001
        service_type = ""
    try:
        from api.portal.views_members import MembersListView
        team = (MembersListView()._case_team_map([client]) or {}).get(str(client.client_id), "") or ""
    except Exception:  # noqa: BLE001
        team = ""
    try:
        ticket_types = [
            c for c in Ticket.objects.filter(
                client_id=client.pk,
                status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
            ).values_list("type__code", flat=True).distinct() if c
        ]
    except Exception:  # noqa: BLE001
        ticket_types = []

    return {
        "eligibility": row.get("eligibility") or "",
        # Coverage gates (NOT stored as columns -- popped in build_row and used
        # for the Company Status "not eligible" test = no valid Medicaid OR no
        # valid social care). Default True so a serializer miss doesn't wrongly
        # flag someone Unable.
        "_has_valid_medicaid": bool(row.get("has_valid_medicaid", True)),
        "_has_valid_social_care": bool(row.get("has_valid_social_care", True)),
        "verification_state": row.get("verification_state") or "",
        "program_status": row.get("program_status_label") or "",
        "lead_source": row.get("lead_source") or "",
        "out_of_orbit": bool(row.get("out_of_orbit")),
        "out_of_range": bool(row.get("out_of_range")),
        "paused": bool(row.get("paused")),
        "pause_type": row.get("pause_type") or "",
        "tags": [t["name"] for t in (row.get("tags") or []) if t.get("name")],
        "service_type": service_type,
        "team": team,
        "ticket_types": ticket_types,
    }


# Company Status: an INDEPENDENT per-member roll-up for the data team -- derived
# from the raw facts (verification, service authorization, nutrition approval,
# block flags, case open/closed), NOT from the service/program status. PRIORITY
# ORDER (first match wins); tell me to reorder if the business wants otherwise:
#   no_case  -> no governing internal-service (food) case ever
#   closed   -> governing case is closed/cancelled
#   unable   -> case OPEN but cannot be delivered (out of orbit / out of range /
#               ineligible)
#   paused   -> case OPEN, service paused (member Paused or enrollment On Hold)
#   pending  -> case OPEN but not yet cleared through verification / service
#               authorization / nutritional approval
#   active   -> being delivered OR ready to be assigned to a kitchen
#               (in any PO, or assigned / ready to be assigned to a kitchen)


def _company_status(enrollment, case, parity, in_any_po, has_medicaid, has_social,
                    member_status="", in_household=False, has_active_delivery=False):
    if case is None or getattr(case, "case_type", "") != _INTERNAL_SERVICE:
        # No internal-service case (own OR household -- the household fallback
        # already ran). A member who is part of a household but has no food case
        # is a household relative, NOT a standalone "No Case Created" member, so
        # leave them uncounted (blank). Only a SOLO caseless member is No Case.
        return "" if in_household else "no_case"
    if (getattr(case, "case_status", "") or "").lower() in ("closed", "cancelled"):
        return "closed"
    # --- governing case is OPEN below ---
    # enrollment may be None (an open internal-service case but the member never
    # started verification -- navigation). Read enrollment fields defensively.
    auth = (getattr(case, "service_authorization_status", "") or "").lower()
    stage = (getattr(enrollment, "stage", "") or "") if enrollment else ""
    verified = (getattr(enrollment, "verified_at", None) is not None) if enrollment else False
    nutrition_ok = (getattr(enrollment, "nutritionist_approved_at", None) is not None) if enrollment else False
    prog = parity.get("program_status") or ""
    # Unable = cannot be delivered though the case is open: out of orbit/range,
    # NOT ELIGIBLE (no valid Medicaid/social OR the hard lifecycle INELIGIBLE
    # off-ramp), a DENIED authorization, OR an EXPIRED authorization (approval
    # window lapsed -> needs reauthorization; raw status can still read "approved",
    # so we key off the computed program_status).
    not_eligible = (
        (not has_medicaid) or (not has_social)
        or parity.get("eligibility") == "ineligible"
    )
    if (parity.get("out_of_orbit") or parity.get("out_of_range")
            or not_eligible or auth == "denied"
            or prog == "Authorization Expired"):
        return "unable"
    # Paused = made it through but service paused, case still open -- by an AGENT
    # (member status Paused), a NUTRITIONIST (member status Nutritionist Paused),
    # OR the program is ON HOLD. (Eligibility pauses lack coverage and were caught
    # above as Unable.)
    if (parity.get("paused") or member_status == "nutritionist_paused"
            or stage == "on_hold" or prog == "On Hold"):
        return "paused"
    # Active = who we're ACTUALLY serving RIGHT NOW, and ONLY on a REAL completed
    # verification (verified_at -- NOT merely inferred from the stage, which the
    # reconcile incident proved can be false) AND a currently-VALID authorization
    # (approved / not-required; an EXPIRED auth already fell into Unable above).
    # Two ways to be Active:
    #   * BEING DELIVERED -- an active delivery calendar (a non-cancelled
    #     DeliveryOrder in a PO). Nutrition need NOT be re-checked here: the
    #     member is already being served, and any nutrition gap still surfaces in
    #     the nutritionist filter. (This is deliberately the delivery calendar,
    #     NOT the `service_active` stage -- an activated member with no live
    #     deliveries is not actually being served.)
    #   * PENDING KITCHEN ASSIGNMENT -- not yet delivered, so require the
    #     nutritionist sign-off before counting them Active.
    if verified and auth in ("approved", "not_required"):
        if has_active_delivery:
            return "active"
        if stage == "kitchen_assignment" and nutrition_ok:
            return "active"
    # Pending = open case + a LIVE authorization (approved OR pending/requested)
    # + still PROGRESSING toward service, i.e. a PRE-service enrollment: pending
    # verification / verified (awaiting) / pending nutritionist -- everything
    # BEFORE actually being served. Deliberately NOT service_active/complete.
    if auth in ("approved", "pending") and stage not in (
        "service_active", "service_complete",
    ):
        return "pending"
    # REVIEW (temporary bucket, excluded from Pending -- to be resolved, see
    # docs/company-status-review-activated-no-delivery.md):
    #   * "activated but not delivering": a service_active/complete enrollment
    #     with NO live delivery calendar (Active requires an active calendar), and
    #   * authorizations that aren't approved/pending (e.g. never_requested).
    return "review"


def _nutritionist_status(enrollment):
    """Nutrition-review status: 'approved' once a nutritionist has signed off,
    else 'pending' for a verified member awaiting sign-off, else '' (not yet in
    the nutrition queue / no enrollment)."""
    if enrollment is None:
        return ""
    if enrollment.nutritionist_approved_at is not None:
        return "approved"
    if enrollment.verified_at is not None:
        return "pending"
    return ""


def _active_enrollment(client):
    """The member's active/governing enrollment (own or household), or None --
    reuses the same helper the Members list uses for parity."""
    from api.portal import serializers as s
    try:
        return s.active_enrollment(client)
    except Exception:  # noqa: BLE001
        return None


def build_row(client):
    """Compute the EnrollmentAnalytics field dict for one MEMBER (client). The
    member may have no enrollment / no internal-service case (company_status =
    no_case); enrollment-specific fields are then blank."""
    cid = client.client_id
    membership = getattr(client, "household_membership", None)
    enr = _active_enrollment(client)  # may be None

    cur_del, last_po_del, last_delivered, delivery_company = _derive_delivery(cid)
    in_any_po = _in_any_po(cid)
    has_active_delivery = _has_active_delivery(cid)
    ins_status, ins_exp, soc_status, soc_exp = _coverage(cid)
    if enr is not None:
        menu_type, allergies, conditions, meds, member_status = _dietary(enr.pk, cid)
    else:
        menu_type, allergies, conditions, meds, member_status = "", [], [], [], ""
    has_scr, scr_at, has_asm, asm_at, eligible = _screening_assessment(cid)
    parity = _parity_fields(client)
    # Coverage gates for the Company Status "not eligible" test -- pop so they
    # aren't written as (nonexistent) columns.
    has_medicaid = parity.pop("_has_valid_medicaid", True)
    has_social = parity.pop("_has_valid_social_care", True)

    # Governing internal-service case -- the SAME canonical resolution the
    # Members/Verification pages use: the client's own governing IS case
    # (favorability + deferral aware), else their household's. Keeps auth_status /
    # case_status / company_status in agreement with those pages for members with
    # multiple internal-service cases.
    from api.portal.serializers import governing_service_case_for_display
    case = governing_service_case_for_display(client)

    # Verification-page parity flags. These reproduce the Verification page's
    # EXACT Pending / Verified buckets so the Data page's verification filter
    # matches it household-for-household. Both require the page's scope
    # (require_internal_service_primary: the member's household PRIMARY holds an
    # OPEN internal-service case):
    #   has_verified_enrollment  = scope AND any own/household enrollment verified
    #                              (== verification_completed_q)
    #   has_pending_verification_enrollment = scope AND has an own/household
    #     enrollment at pending_verification AND lifecycle_stage in the verify
    #     window AND NOT verified.
    _VWINDOW = ("pending_verification", "verified", "kitchen_assignment")
    funnel_enr = list(client.enrollments.all())
    if membership is not None:
        funnel_enr += list(membership.household.enrollment_verifications.all())
    verified_completed = any(e.verified_at is not None for e in funnel_enr)
    has_pending_enr = any((e.stage or "") == "pending_verification" for e in funnel_enr)
    lifecycle_in_window = (getattr(client, "lifecycle_stage", "") or "") in _VWINDOW
    # Scope: the household PRIMARY holds an OPEN internal-service case.
    primary_open_is = False
    if membership is not None:
        primary_m = next(
            (m for m in membership.household.members.all() if m.is_primary and m.client_id),
            None,
        )
        if primary_m is not None:
            primary_open_is = any(
                c.case_type == _INTERNAL_SERVICE
                and (c.case_status or "").lower() not in ("closed", "cancelled")
                for c in primary_m.client.cases.all()
            )
    has_verified_enr = primary_open_is and verified_completed
    has_pending_verif = (
        primary_open_is and has_pending_enr and lifecycle_in_window
        and not verified_completed
    )

    # Verified-by, mirroring the page fallback: "System" when verified with no agent.
    verified_at = enr.verified_at if enr is not None else None
    verified_by_name = ""
    if verified_at is not None:
        vb = enr.verified_by
        verified_by_name = (
            (vb.name or "").strip() or (vb.agent_code or "") if vb else ""
        ) or "System"

    row = {
        "enrollment_id": (enr.pk if enr is not None else None),
        "household_id": (
            enr.household_id if enr is not None
            else (membership.household_id if membership else None)
        ),
        "case_id": (case.case_id if case else None),
        "is_primary": bool(membership.is_primary) if membership else False,
        "stage": (enr.stage or "") if enr is not None else "",
        "first_name": client.first_name or "",
        "last_name": client.last_name or "",
        "medicaid_id": getattr(client, "medicaid_id", "") or "",
        "dob": client.date_of_birth,
        "member_created_at": client.created_at,
        "care_coordinator": client.care_coordinator or "",
        "primary_care_coordinator": (case.primary_worker_name if case else "") or "",
        "cadence": _cadence_from_weekdays(enr.delivery_weekdays if enr is not None else []),
        "kitchen_id": (enr.kitchen_id if enr is not None else None),
        "kitchen_name": (enr.kitchen.name if (enr is not None and enr.kitchen_id) else ""),
        "menu_type": menu_type,
        "current_delivery_status": cur_del,
        "last_po_delivery_status": last_po_del,
        "last_delivered_at": last_delivered,
        "in_any_po": in_any_po,
        "has_pending_verification_enrollment": has_pending_verif,
        "has_verified_enrollment": has_verified_enr,
        "insurance_status": ins_status or "",
        "insurance_expires_at": ins_exp,
        "social_status": soc_status or "",
        "social_expires_at": soc_exp,
        "attestation_status": "needed" if getattr(client, "attestation_needed", False) else "",
        "attestation_requested_at": None,   # backfilled from CRM later
        "attestation_completed_at": None,
        "has_screening": has_scr,
        "screening_at": scr_at,
        "has_eligibility_assessment": has_asm,
        "eligibility_assessment_at": asm_at,
        "verified_at": verified_at,
        "verified_by_name": verified_by_name,
        "case_type": (case.case_type if case else ""),
        "case_status": (case.case_status if case else ""),
        "auth_status": (case.service_authorization_status if case else ""),
        "case_opened_at": (case.date_opened if case else None),
        "program_name": (case.program_name if case else ""),
        # Program TYPE = the governing case's household_type (household /
        # individual) -- drives the Data page's "Program (Household/Individual)"
        # filter. Was never populated (always blank), so that filter matched
        # nothing; keyed off the governing internal-service case like the rest.
        "program_type": (case.household_type if case else ""),
        "allergies": allergies,
        "medical_conditions": conditions,
        "medications": meds,
        "eligible_services": eligible,
        # Data-team criteria straight off the enrollment/case.
        "verified_by_id_str": (
            str(enr.verified_by_id) if (enr is not None and enr.verified_by_id) else ""
        ),
        "requested_at": (
            (enr.requested_at or enr.opened_at) if enr is not None else None
        ),
        "case_closed_at": (case.case_closed_at if case else None),
        # Members-parity criteria (eligibility / status / flags / tags / ...).
        **parity,
        # Data-team roll-up bucket (independent: raw verification/auth/nutrition
        # + block flags + case state).
        "company_status": _company_status(
            enr, case, parity, in_any_po, has_medicaid, has_social, member_status,
            in_household=membership is not None, has_active_delivery=has_active_delivery,
        ),
        # Nutrition-review status + delivery company on latest order.
        "nutritionist_status": _nutritionist_status(enr),
        "delivery_company": delivery_company,
    }

    # NO internal-service case (No Case Created) -> the member has no food-program
    # engagement to describe, so blank every case/enrollment/delivery-derived
    # field. Any residual here is orphaned data (e.g. a deleted internal-service
    # case leaving DeliveryOrders behind -- see
    # docs/known-issue-orphaned-delivery-no-case.md). Case-independent fields
    # (identity, eligibility, screening, coverage, attestation, tags) are kept.
    if case is None:
        row.update({
            "enrollment_id": None, "stage": "", "cadence": "",
            "kitchen_id": None, "kitchen_name": "", "menu_type": "",
            "current_delivery_status": "", "last_po_delivery_status": "",
            "last_delivered_at": None, "in_any_po": False, "delivery_company": "",
            "verified_at": None, "verified_by_name": "", "verified_by_id_str": "",
            "requested_at": None, "nutritionist_status": "",
            "case_type": "", "case_status": "", "auth_status": "",
            "case_opened_at": None, "case_closed_at": None, "program_name": "",
            "service_type": "", "program_type": "", "program_status": "",
            "verification_state": "", "out_of_orbit": False, "out_of_range": False,
            "paused": False, "pause_type": "",
            "allergies": [], "medical_conditions": [], "medications": [],
        })
    return row


def _base_qs(client_ids=None):
    qs = Client.objects.select_related("household_membership").prefetch_related(
        # Feed MemberListSerializer + its helpers (+ active_enrollment / dietary /
        # case resolution) without N+1 -- same shape as the Members list's
        # MEMBER_LIST_PREFETCH.
        "insurances", "tags", "addresses", "social_care_coverages",
        "military_profile", "member_profiles", "cases",
        Prefetch(
            "enrollments",
            queryset=EnrollmentVerification.objects.select_related(
                "kitchen", "verified_by", "case",
            ),
        ),
        "household_membership__household__members",
        "household_membership__household__enrollment_verifications",
        # Household primary's cases -> the require_internal_service_primary scope
        # (primary holds an OPEN internal-service case) used by the verification flags.
        "household_membership__household__members__client__cases",
    )
    if client_ids is not None:
        qs = qs.filter(pk__in=client_ids)
    return qs


# The read model is always built/read against the PRIMARY here (never the
# replica), so the rebuild can't compute from -- or dedupe against -- lagging data.
_PRIMARY = "default"


def upsert_client(client):
    """Build + write one member's analytics row (on the primary)."""
    EnrollmentAnalytics.objects.using(_PRIMARY).update_or_create(
        client=client, defaults=build_row(client),
    )


def rebuild(client_ids=None, *, chunk=500, progress=None):
    """(Re)build analytics rows -- ONE PER MEMBER. Full rebuild when ``client_ids``
    is None.

    ``progress(done, total)`` is an optional callback for live progress.
    Returns the number of rows written.
    """
    qs = _base_qs(client_ids)
    total = qs.count()
    done = 0
    for client in qs.iterator(chunk_size=chunk):
        try:
            upsert_client(client)
        except Exception as exc:  # noqa: BLE001 - never let one row kill the run
            logger.warning("enrollment_analytics: client %s failed: %s",
                           client.pk, exc, exc_info=True)
        done += 1
        if progress and done % chunk == 0:
            progress(done, total)
    if progress:
        progress(done, total)
    return done


def _shift_years(d, years):
    """``d`` shifted back ``years`` years (Feb-29 safe)."""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year - years)


def filter_analytics(params):
    """Build the filtered/sorted EnrollmentAnalytics queryset for the Data page
    from request query params. Every filter maps to an indexed column (btree) or
    a GIN array containment (multi-selects), so any combination stays fast."""
    import datetime

    from django.db.models import Q

    qs = EnrollmentAnalytics.objects.all()
    g = lambda k: (params.get(k) or "").strip()  # noqa: E731

    search = g("search")
    if search:
        cond = Q(first_name__icontains=search) | Q(last_name__icontains=search) \
            | Q(medicaid_id__icontains=search)
        try:
            import uuid as _uuid
            cond |= Q(client_id=_uuid.UUID(search)) | Q(enrollment_id=int(search)) \
                if search.isdigit() else cond | Q(client_id=_uuid.UUID(search))
        except (ValueError, AttributeError):
            pass
        qs = qs.filter(cond)

    # Previously vs Never delivered: whether the member (with an OPEN governing
    # internal-service case) has ever been included in a generated PO. Both
    # options are scoped to an open governing case.
    delivered = g("delivered")
    if delivered in ("previously", "never"):
        qs = qs.filter(case_type="internal_service").exclude(
            case_status__in=["closed", "cancelled"]
        ).filter(in_any_po=(delivered == "previously"))

    # Nutritionist filter. "none" is the sentinel for the blank bucket -- members
    # not (yet) at the nutritionist step (still pre-verification) or closed -- so
    # the three options (approved / pending / not-at-step) partition the set.
    ns = g("nutritionist_status")
    if ns == "none":
        qs = qs.filter(nutritionist_status="")
    elif ns:
        qs = qs.filter(nutritionist_status=ns)

    # Verification filter -- matches the VERIFICATION PAGE queue (enrollment-grain,
    # across own + household enrollments), NOT the per-member governing-enrollment
    # scalar. "Pending Verification" = has an enrollment at that stage; "Verified"
    # = has a verified governing enrollment.
    vstate = g("verification_state")
    if vstate == "Pending Verification":
        qs = qs.filter(has_pending_verification_enrollment=True)
    elif vstate == "Verified":
        qs = qs.filter(has_verified_enrollment=True)

    # Internal Service case filter (+ open/closed sub-filter), mirroring the
    # Members list. The read model's case_* fields describe the governing
    # internal-service case.
    if g("has_internal_service") in ("1", "true", "yes"):
        qs = qs.filter(case_type="internal_service")
        istatus = g("internal_status")
        if istatus == "open":
            qs = qs.filter(case_status="open")
        elif istatus == "closed":
            qs = qs.filter(case_status__in=["closed", "cancelled"])

    # Age range -> DOB bounds.
    today = datetime.date.today()
    if g("age_min"):
        qs = qs.filter(dob__lte=_shift_years(today, int(g("age_min"))))
    if g("age_max"):
        qs = qs.filter(dob__gte=_shift_years(today, int(g("age_max")) + 1))

    # Scalar exact-match filters: param -> column.
    for param, col in {
        "care_coordinator": "care_coordinator__icontains",
        "primary_care_coordinator": "primary_care_coordinator__icontains",
        "cadence": "cadence", "kitchen": "kitchen_id", "menu_type": "menu_type",
        "current_delivery_status": "current_delivery_status",
        "last_po_delivery_status": "last_po_delivery_status",
        "insurance_status": "insurance_status", "social_status": "social_status",
        "attestation_status": "attestation_status", "stage": "stage",
        "case_type": "case_type", "case_status": "case_status",
        "auth_status": "auth_status", "program": "program_name",
        "company_status": "company_status",
        "delivery_company": "delivery_company",
        # Members-parity criteria.
        "eligibility": "eligibility",
        "program_status": "program_status", "lead_source": "lead_source",
        "team": "team", "service_type": "service_type",
        "program_type": "program_type", "pause_type": "pause_type",
        "verified_by": "verified_by_id_str",
    }.items():
        if g(param):
            qs = qs.filter(**{col: g(param)})

    # Boolean filters.
    for param, col in {"has_screening": "has_screening",
                       "has_eligibility_assessment": "has_eligibility_assessment",
                       "out_of_orbit": "out_of_orbit", "out_of_range": "out_of_range",
                       "paused": "paused"}.items():
        if g(param) in ("1", "true", "yes"):
            qs = qs.filter(**{col: True})
        elif g(param) in ("0", "false", "no"):
            qs = qs.filter(**{col: False})

    # Date-range filters: param prefix -> column.
    for prefix, col in {
        "created": "member_created_at", "delivered": "last_delivered_at",
        "insurance_exp": "insurance_expires_at", "social_exp": "social_expires_at",
        "screening": "screening_at", "assessment": "eligibility_assessment_at",
        "case_opened": "case_opened_at", "requested": "requested_at",
        "verified": "verified_at", "closed": "case_closed_at",
    }.items():
        if g(f"{prefix}_from"):
            qs = qs.filter(**{f"{col}__date__gte": g(f"{prefix}_from")})
        if g(f"{prefix}_to"):
            qs = qs.filter(**{f"{col}__date__lte": g(f"{prefix}_to")})

    # Multi-select array filters (GIN __overlap = "matches ANY of").
    for param, col in {"allergies": "allergies", "medical_conditions": "medical_conditions",
                       "medications": "medications", "eligible_services": "eligible_services",
                       "tags": "tags", "ticket_types": "ticket_types"}.items():
        vals = [v.strip() for v in g(param).split(",") if v.strip()]
        if vals:
            qs = qs.filter(**{f"{col}__overlap": vals})

    # Sort.
    sort_map = {"created": "member_created_at", "delivered": "last_delivered_at",
                "name": "last_name", "verified": "verified_at"}
    col = sort_map.get(g("sort"), "member_created_at")
    if (params.get("dir") or "desc").lower() != "asc":
        col = "-" + col
    return qs.order_by(col, "last_name", "first_name")


def prune_orphans():
    """Delete analytics rows whose member (Client) no longer exists (CASCADE
    already handles hard deletes; this is a safety net for the nightly reconcile)."""
    live = set(Client.objects.values_list("pk", flat=True))
    stale = [
        pk for pk in EnrollmentAnalytics.objects.using(_PRIMARY)
        .values_list("client_id", flat=True)
        if pk not in live
    ]
    if stale:
        EnrollmentAnalytics.objects.using(_PRIMARY).filter(
            client_id__in=stale
        ).delete()
    return len(stale)
