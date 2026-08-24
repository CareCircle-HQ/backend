"""Build/refresh the EnrollmentAnalytics read model (Administration > Data page).

One row per EnrollmentVerification, flattening every Data-page filter field from
the live tables (incl. derived delivery status + multi-valued dietary/eligibility
arrays). Rebuilt on a schedule (~hourly) -- see tasks.rebuild_enrollment_analytics
and docs/analytics-architecture.md. Not the source of truth.
"""

import logging

from django.utils import timezone

from ..models import (
    Assessment, DeliveryOrder, EnrollmentAnalytics, EnrollmentVerification,
    Insurance, MemberDietaryProfile, Screening, SocialCareCoverage,
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
        .select_related("purchase_order")
        .order_by("-expected_delivery_date", "-created_at")[:50]
    )
    if not orders:
        return "", "", None
    latest = orders[0]
    current = latest.status or ""
    last_po = (latest.purchase_order.delivery_status
               if latest.purchase_order else "") or ""
    delivered = next(
        (o.delivered_at or o.expected_delivery_date
         for o in orders if o.status == "delivered"), None
    )
    return current, last_po, delivered


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
        return "", [], [], []
    return (
        prof.menu_type or "",
        _clean(prof.food_allergies), _clean(prof.conditions), _clean(prof.medications),
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


def build_row(enrollment):
    """Compute the EnrollmentAnalytics field dict for one enrollment."""
    client = enrollment.client
    cid = enrollment.client_id
    membership = getattr(client, "household_membership", None)

    cur_del, last_po_del, last_delivered = _derive_delivery(cid)
    ins_status, ins_exp, soc_status, soc_exp = _coverage(cid)
    menu_type, allergies, conditions, meds = _dietary(enrollment.pk, cid)
    has_scr, scr_at, has_asm, asm_at, eligible = _screening_assessment(cid)

    # Governing internal-service case: the enrollment's own case when it's
    # internal-service, else the client's most-recent internal-service case.
    case = enrollment.case if getattr(enrollment.case, "case_type", "") == _INTERNAL_SERVICE else None
    if case is None:
        case = (client.cases.filter(case_type=_INTERNAL_SERVICE)
                .order_by("-date_opened").first())

    # Verified-by, mirroring the page fallback: "System" when verified with no agent.
    verified_by_name = ""
    if enrollment.verified_at is not None:
        vb = enrollment.verified_by
        verified_by_name = (
            (vb.name or "").strip() or (vb.agent_code or "") if vb else ""
        ) or "System"

    return {
        "client_id": cid,
        "household_id": enrollment.household_id,
        "case_id": (case.case_id if case else None),
        "is_primary": bool(membership.is_primary) if membership else False,
        "stage": enrollment.stage or "",
        "first_name": client.first_name or "",
        "last_name": client.last_name or "",
        "medicaid_id": getattr(client, "medicaid_id", "") or "",
        "dob": client.date_of_birth,
        "member_created_at": client.created_at,
        "care_coordinator": client.care_coordinator or "",
        "primary_care_coordinator": (case.primary_worker_name if case else "") or "",
        "cadence": _cadence_from_weekdays(enrollment.delivery_weekdays),
        "kitchen_id": enrollment.kitchen_id,
        "kitchen_name": (enrollment.kitchen.name if enrollment.kitchen_id else ""),
        "menu_type": menu_type,
        "current_delivery_status": cur_del,
        "last_po_delivery_status": last_po_del,
        "last_delivered_at": last_delivered,
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
        "verified_at": enrollment.verified_at,
        "verified_by_name": verified_by_name,
        "case_type": (case.case_type if case else ""),
        "case_status": (case.case_status if case else ""),
        "auth_status": (case.service_authorization_status if case else ""),
        "case_opened_at": (case.date_opened if case else None),
        "program_name": (case.program_name if case else ""),
        "allergies": allergies,
        "medical_conditions": conditions,
        "medications": meds,
        "eligible_services": eligible,
    }


def _base_qs(enrollment_ids=None):
    qs = EnrollmentVerification.objects.select_related(
        "client", "case", "kitchen", "client__household_membership",
    )
    if enrollment_ids is not None:
        qs = qs.filter(pk__in=enrollment_ids)
    return qs


# The read model is always built/read against the PRIMARY here (never the
# replica), so the rebuild can't compute from -- or dedupe against -- lagging data.
_PRIMARY = "default"


def upsert_enrollment(enrollment):
    """Build + write one enrollment's analytics row (on the primary)."""
    EnrollmentAnalytics.objects.using(_PRIMARY).update_or_create(
        enrollment=enrollment, defaults=build_row(enrollment),
    )


def rebuild(enrollment_ids=None, *, chunk=500, progress=None):
    """(Re)build analytics rows. Full rebuild when ``enrollment_ids`` is None.

    ``progress(done, total)`` is an optional callback for live progress.
    Returns the number of rows written.
    """
    qs = _base_qs(enrollment_ids)
    total = qs.count()
    done = 0
    for enr in qs.iterator(chunk_size=chunk):
        try:
            upsert_enrollment(enr)
        except Exception as exc:  # noqa: BLE001 - never let one row kill the run
            logger.warning("enrollment_analytics: row %s failed: %s", enr.pk, exc,
                           exc_info=True)
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
        "auth_status": "auth_status",
    }.items():
        if g(param):
            qs = qs.filter(**{col: g(param)})

    # Boolean filters.
    for param, col in {"has_screening": "has_screening",
                       "has_eligibility_assessment": "has_eligibility_assessment"}.items():
        if g(param) in ("1", "true", "yes"):
            qs = qs.filter(**{col: True})
        elif g(param) in ("0", "false", "no"):
            qs = qs.filter(**{col: False})

    # Date-range filters: param prefix -> column.
    for prefix, col in {
        "created": "member_created_at", "delivered": "last_delivered_at",
        "insurance_exp": "insurance_expires_at", "social_exp": "social_expires_at",
        "screening": "screening_at", "assessment": "eligibility_assessment_at",
        "case_opened": "case_opened_at",
    }.items():
        if g(f"{prefix}_from"):
            qs = qs.filter(**{f"{col}__date__gte": g(f"{prefix}_from")})
        if g(f"{prefix}_to"):
            qs = qs.filter(**{f"{col}__date__lte": g(f"{prefix}_to")})

    # Multi-select array filters (GIN __overlap = "matches ANY of").
    for param, col in {"allergies": "allergies", "medical_conditions": "medical_conditions",
                       "medications": "medications", "eligible_services": "eligible_services"}.items():
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
    """Delete analytics rows whose enrollment no longer exists (CASCADE already
    handles hard deletes; this is a safety net for the nightly reconcile)."""
    live = set(EnrollmentVerification.objects.values_list("pk", flat=True))
    stale = [
        pk for pk in EnrollmentAnalytics.objects.using(_PRIMARY)
        .values_list("enrollment_id", flat=True)
        if pk not in live
    ]
    if stale:
        EnrollmentAnalytics.objects.using(_PRIMARY).filter(
            enrollment_id__in=stale
        ).delete()
    return len(stale)
