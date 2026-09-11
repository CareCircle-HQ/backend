"""Executive dashboard -- Member & Case counts, every one a Data-page filter.

The spec ("Executive Dashboard Breakdown") defines each tile as a combination of
DATA PAGE filters, so every number here runs through
:func:`api.services.enrollment_analytics.filter_analytics` -- the same engine the
Data page uses -- instead of re-deriving anything. That is the point: an
executive number that disagrees with the page an agent opens to check it is worse
than no number, and re-implementing "active" a second way is exactly how that
happens.

Cheap enough to do honestly: a count is ~3ms against the indexed read model, so
the whole dashboard is a fraction of a second.

DATE RANGE. The header range filters MEMBER CREATED -- when the member reached
US (``Client.client_added_at``, the read model's ``member_added_at``) -- via the
Data page's own ``created_from`` / ``created_to`` params. It applies to every
tile, cases included: a case is counted through the member row it governs, so
one date rule scopes the whole page.

FRESHNESS. Reads the EnrollmentAnalytics read model, rebuilt periodically, so the
payload carries ``as_of``. The dashboard is therefore exactly as current as the
Data page -- including being equally stale when a rebuild has not run.
"""

from django.db.models import Count, Max

from api.models import Case, EnrollmentAnalytics
from api.services.enrollment_analytics import (
    ANY_ASSIGNED, NOT_ASSIGNED, filter_analytics,
)

# Company Status values (the Data page's filter, computed in
# enrollment_analytics._company_status).
ACTIVE = "active"
PENDING = "pending"
UNABLE = "unable"
PAUSED = "paused"
CLOSED = "closed"
NO_CASE = "no_case"
REVIEW = "review"

# The four "inactive but a case exists" states, plus REVIEW. Review is a
# temporary quarantine, not a business state (see
# docs/company-status-review-activated-no-delivery.md): mostly members activated
# with no delivery calendar. It is counted here so the four header cards still
# sum to Total Members -- those members DO have a case and are not being served
# -- while also getting its own visible row below, so a data problem cannot hide
# inside an aggregate.
INACTIVE_WITH_CASE = (PENDING, UNABLE, PAUSED, CLOSED, REVIEW)

# Insurance / social-care buckets that mean "not covered". Each is a single
# column value, so they are mutually exclusive and summing them is exact (no
# member is double counted). NOT_ASSIGNED is the blank "nothing on file" bucket.
NO_INSURANCE = ("inactive", "expired", NOT_ASSIGNED)
NO_SOCIAL_CARE = ("non_enrolled", "expired", NOT_ASSIGNED)


def _date_params(params):
    """Just the member-created range, which every tile shares."""
    out = {}
    for key in ("created_from", "created_to"):
        val = (params.get(key) or "").strip()
        if val:
            out[key] = val
    return out


def _service_rows(base, extra_by_row):
    """meals/boxes x the row breakdown, as the design lays each status card out."""
    out = {}
    for service in ("meals", "boxes"):
        rows = {"total": _count(base, service_type=service)}
        for name, extra in extra_by_row.items():
            rows[name] = _count(base, service_type=service, **extra)
        out[service] = rows
    return out


_COUNTER = {"n": 0}


def _count(base, **extra):
    """One tile: merge the shared filters with this tile's own and count."""
    _COUNTER["n"] += 1
    return filter_analytics({**base, **extra}).count()


def _active(base):
    """ACTIVE, split by service and how far along delivery is.

    "Assigned for delivery" is a kitchen but nothing delivered yet, which needs
    the ANY_ASSIGNED sentinel -- NOT_ASSIGNED alone cannot express it and a
    specific kitchen id would narrow it to one kitchen.
    """
    b = {**base, "company_status": ACTIVE}
    return {
        "total": _count(b),
        **_service_rows(b, {
            "being_delivered": {"delivered": "previously"},
            "assigned_for_delivery": {"delivered": "never", "kitchen": ANY_ASSIGNED},
            "not_assigned_for_delivery": {"delivered": "never", "kitchen": NOT_ASSIGNED},
        }),
    }


def _pending(base):
    """PENDING, split by service and which gate they are waiting on.

    The design's three rows covered only 79 of 162 Pending members, so two more
    were added to account for the rest: members whose nutritionist review came
    back with QUESTIONS (a different state from waiting for approval), and those
    whose verification was NEVER REQUESTED. With those, the rows use the Data
    page's own verification vocabulary, which partitions Pending exactly
    (Pending Verification + Verified + Never Requested + Not Applicable = all).
    """
    b = {**base, "company_status": PENDING}
    return {
        "total": _count(b),
        **_service_rows(b, {
            "pending_authorization": {"auth_status": "pending"},
            "approved_pending_verification": {
                "auth_status": "approved",
                "verification_state": "Pending Verification",
            },
            "verified_pending_nutrition": {
                "auth_status": "approved",
                "verification_state": "Verified",
                "nutritionist_status": "waiting_approval",
            },
            "nutrition_questions_outstanding": {
                "auth_status": "approved",
                "verification_state": "Verified",
                "nutritionist_status": "pending_questions",
            },
            "verification_never_requested": {
                "verification_state": "Never Requested",
            },
        }),
    }


def _unable(base):
    """UNABLE TO BE SERVICED, by reason.

    The header is the DISTINCT member count, so it equals what the Data page
    shows for Company Status = Unable to Be Serviced. It is deliberately not the
    sum of the rows: the reasons overlap heavily (On Hold alone covers most of
    them), so summing reported 2,935 against a real 1,844 and the dashboard
    disagreed with the page an agent opens to check it -- the one thing this
    dashboard must never do. ``rows_sum`` is returned so the overlap can be
    stated instead of leaving two numbers that look wrong together.

    The design's five reasons only accounted for 1,160 of 1,844 Unable members.
    ``_company_status`` also sends a member here for being OUT OF ORBIT (the
    kitchen cannot fulfil their menu/allergies) or on an EXPIRED authorization,
    and On Hold members land here when another gate fails -- none of which had a
    row. Those three were added, so the reasons cover the status instead of the
    header silently understating it.
    """
    b = {**base, "company_status": UNABLE}
    rows = {
        "rejected_case_open": _count(b, eligibility="ineligible"),
        "out_of_range": _count(b, out_of_range="1"),
        # Multi-bucket reasons: exclusive column values, so a sum is exact.
        "expired_or_no_insurance": sum(
            _count(b, insurance_status=v) for v in NO_INSURANCE
        ),
        "expired_or_no_social_care": sum(
            _count(b, social_status=v) for v in NO_SOCIAL_CARE
        ),
        "case_rejected": _count(b, auth_status="denied"),
        "out_of_orbit": _count(b, out_of_orbit="1"),
        "on_hold": _count(b, program_status="On Hold"),
        "authorization_expired": _count(b, program_status="Authorization Expired"),
    }
    return {"total": _count(b), "rows_sum": sum(rows.values()), **rows}


def _delivery_history(base, status):
    """PAUSED / CLOSED, split by whether they were ever actually delivered to."""
    b = {**base, "company_status": status}
    return {
        "total": _count(b),
        "previously_delivered": _count(b, delivered="previously"),
        "never_delivered": _count(b, delivered="never"),
    }


def _by_member_type(base):
    """Primary members vs members of household, ACTIVE only.

    Three mutually exclusive groups that partition Active, which is what makes
    the section reconcile to the Active card:
      * Individual        -- an individual-program case
      * Primary household -- the household case holder
      * Members of household -- everyone else on a household case
    The spec omitted the primary/non-primary split on the household rows; the
    design's arithmetic (individual + primary household = "Primary Members", and
    that plus members-of-household = Active) only works this way.
    """
    b = {**base, "company_status": ACTIVE}
    individual = {"total": _count(b, program_type="individual")}
    primary_hh = {"total": _count(b, program_type="household", primary_member="primary")}
    household = {"total": _count(b, program_type="household", primary_member="additional")}
    for service in ("meals", "boxes"):
        individual[service] = _count(b, program_type="individual", service_type=service)
        primary_hh[service] = _count(
            b, program_type="household", primary_member="primary", service_type=service,
        )
        household[service] = _count(
            b, program_type="household", primary_member="additional",
            service_type=service,
        )
    return {
        "primary_members": {
            "total": individual["total"] + primary_hh["total"],
            "individual": individual,
            "primary_household_members": primary_hh,
        },
        "members_of_household": household,
    }


def _cases(base):
    """Case-grain counts, derived from the SAME filtered member rows.

    The read model is member-grain but carries each member's governing
    ``case_id``, so counting DISTINCT case_id gives governing cases while keeping
    the member-created date scoping and the Data page's governing rule.

    That rule is the Data page's (``governing_service_case_for_display``), which
    is NOT the stricter one the old dashboard used
    (``governing_internal_case_ids``, where a blank/never-requested
    authorization can never govern). They differ by a few hundred cases; the
    brief was Data-page parity.

    Non-governing = every other case of those members, whatever the pipeline
    (navigation / eligibility, plus extra internal-service cases that lost to the
    governing one).
    """
    rows = filter_analytics(base)
    governing_ids = set(rows.exclude(case_id=None).values_list("case_id", flat=True))
    client_ids = rows.values_list("client_id", flat=True)

    non_governing = (
        Case.objects.filter(client_id__in=client_ids)
        .exclude(pk__in=governing_ids)
        .count()
    )

    def by(column):
        # .order_by() CLEARS the sort filter_analytics applies: Django adds
        # ordering columns to the GROUP BY, which fragments every group into rows
        # of one (each member's own row) instead of grouping by the column.
        grouped = (
            rows.exclude(case_id=None)
            .order_by()
            .values(column)
            .annotate(n=Count("case_id", distinct=True))
        )
        return {(row[column] or ""): row["n"] for row in grouped}

    auth = by("auth_status")
    status = by("case_status")
    service = by("service_type")
    return {
        "total": {
            "governing": len(governing_ids),
            "non_governing": non_governing,
        },
        # Requested/Rejected are the design's words for the stored pending/denied.
        # "Never requested" gets its own row rather than being folded into
        # Requested or silently dropped -- those cases exist (an authorization was
        # never asked for) and are not the same thing as a pending request.
        "by_authorization": {
            "requested": auth.get("pending", 0),
            "approved": auth.get("approved", 0),
            "rejected": auth.get("denied", 0),
            "never_requested": auth.get("never_requested", 0),
        },
        "by_status": {
            "open": status.get("open", 0),
            "closed": status.get("closed", 0) + status.get("cancelled", 0),
        },
        "by_service_type": {
            "meals": service.get("meals", 0),
            "boxes": service.get("boxes", 0),
        },
    }


def build_executive_dashboard(params):
    """The whole payload. ``params`` accepts the Data page's created_from/_to."""
    _COUNTER["n"] = 0
    base = _date_params(params)

    total = _count(base)
    active = _count(base, company_status=ACTIVE)
    with_case_states = sum(
        _count(base, company_status=s) for s in INACTIVE_WITH_CASE
    )
    no_case = _count(base, company_status=NO_CASE)

    # The header percentages are of members WITH a case -- the population the
    # company actually serves or is trying to serve. Against Total Members they
    # would be dominated by the imported no-case backlog and read as ~24%.
    denominator = active + with_case_states

    def pct(n):
        return round(n * 100.0 / denominator, 1) if denominator else 0.0

    return {
        # When the read model was last rebuilt, so a stale page says so instead of
        # presenting old counts as current.
        "as_of": EnrollmentAnalytics.objects.aggregate(t=Max("refreshed_at"))["t"],
        "range": {
            "from": base.get("created_from") or "",
            "to": base.get("created_to") or "",
        },
        "totals": {
            "total_members": total,
            "active_members": active,
            "inactive_with_case": with_case_states,
            "inactive_no_case": no_case,
            "with_case": denominator,
            "active_pct": pct(active),
            "inactive_with_case_pct": pct(with_case_states),
        },
        "members": {
            "active": _active(base),
            "pending": _pending(base),
            "unable": _unable(base),
            "paused": _delivery_history(base, PAUSED),
            "closed": _delivery_history(base, CLOSED),
            "no_case": _count(base, company_status=NO_CASE),
            # Own row, per the brief, so the quarantine stays visible.
            "review": _count(base, company_status=REVIEW),
        },
        "by_member_type": _by_member_type(base),
        "cases": _cases(base),
        # How many tile counts produced this payload -- a cheap regression guard
        # on the cost of the page (each is ~3ms against the read model).
        "tile_count": _COUNTER["n"],
    }
