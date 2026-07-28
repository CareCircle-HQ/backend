"""Member / household warning detection.

Single source of truth for the health checks surfaced on the member profile
header and the Customer Service -> Care Management page. Detection logic lives
ONLY here so the on-read scan, the import hook, the extension-save hook and the
nightly sweep can never drift.

Design:
  * Each check is a small function ``check_*(ctx) -> list[Warning]`` registered
    in :data:`WARNING_CHECKS`. Adding a future check = write one function and
    append it to the registry; it automatically flows to the profile header,
    the persisted snapshot and the Care Management page.
  * Checks are pure and read-only here (no persistence, no writes). Phase 2
    (``warnings_sync``) persists a snapshot by calling
    :func:`evaluate_enrollment_warnings`.
  * Severity is a small ordered vocabulary (``red`` > ``orange``) mapped to a
    colour in the UI; add more levels here without touching the checks.

Scope:
  * ``household`` warnings describe the enrollment/household (cadence, kitchen,
    product type, cases) and attach to the primary client; the UI shows them on
    every member of the household.
  * ``member`` warnings describe one client (e.g. insurance) and attach to that
    client only.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

from api.models import (
    EnrollmentStage,
    InsurancePlanType,
    KitchenProductType,
    MemberStatus,
    ProductType,
    ProductTypeKind,
    RecordStatus,
    SERVICE_EXCLUDED_MEMBER_STATUSES,
    ServiceAuthorizationStatus,
    Ticket,
    TicketStatus,
)
from api.services.catalog import (
    product_kind_for_enrollment,
    product_type_kind_for_name,
)
from api.services.delivery import current_household_cadence
from api.services.lifecycle import (
    case_is_met_council,
    governing_internal_case,
    open_internal_service_cases,
    verification_completed,
)

logger = logging.getLogger(__name__)

# --- Severity vocabulary (ordered; extend without touching checks) ---------
RED = "red"
ORANGE = "orange"
SEVERITY_RANK = {RED: 2, ORANGE: 1}

# How early (days before the coverage end date) an insurance is flagged as
# "expiring" (confirmed with product: 30 days).
INSURANCE_EXPIRING_DAYS = 30

# --- Stable warning codes --------------------------------------------------
MULTIPLE_OPEN_CASES = "multiple_open_cases"
CONFLICTING_PRODUCT_TYPES = "conflicting_product_types"
NO_KITCHEN = "no_kitchen"
NO_CADENCE = "no_cadence"
CADENCE_NOT_SUPPORTED_BY_KITCHEN = "cadence_not_supported_by_kitchen"
KITCHEN_MISSING_PRODUCT = "kitchen_missing_product"
CADENCE_KIND_MISMATCH = "cadence_kind_mismatch"
INSURANCE_EXPIRING = "insurance_expiring"
# Member holds a Medicaid plan whose TYPE Met Council can't serve (MLTC / MAP /
# FFS in the plan name) and has NO clean Medicaid plan alongside it. CareCircle
# CANNOT fix this in the CRM -- the case must be closed in Unite Us.
WRONG_MEDICAID_TYPE = "wrong_medicaid_type"
INTERNAL_CASE_EXPIRED = "internal_case_expired"

# Medicaid plan-name tokens that mark an unserviceable Medicaid type. Each entry
# is detected either by its abbreviation OR its long-form name, so a plan named
# "...Medicaid FFS" and "...Fee For Service" both match. Word-aware +
# case-insensitive so "MAPD" does not false-match "MAP".
#   PMLTC  -> Partial Managed Long Term Care
#   MLTCP  -> Managed Long Term Care Partial
#   MLTC   -> Managed Long Term Care
#   FFS    -> Fee For Service
#   MAP    -> Medicaid Advantage Plan
_MEDICAID_INELIGIBLE_TERMS = ("PMLTC", "MLTCP", "MLTC", "MAP", "FFS")
_MEDICAID_INELIGIBLE_PHRASES = (
    "Partial Managed Long Term Care",
    "Managed Long Term Care Partial",
    "Managed Long Term Care",
    "Fee For Service",
    "Medicaid Advantage Plan",
)


def _medicaid_bad_pattern():
    """Word-aware alternation over the abbreviations + long-form phrases. Phrase
    spaces are matched as ``\\s+`` so extra/variant whitespace still matches."""
    parts = [re.escape(t) for t in _MEDICAID_INELIGIBLE_TERMS]
    parts += [
        r"\s+".join(re.escape(w) for w in phrase.split())
        for phrase in _MEDICAID_INELIGIBLE_PHRASES
    ]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


_MEDICAID_BAD_RE = _medicaid_bad_pattern()
# Household service state (household-scope).
HOUSEHOLD_ON_HOLD = "household_on_hold"
HOUSEHOLD_CANCELLED = "household_cancelled"
# Household roll-up counts of members not being served (household-scope). Shown
# on EVERY member of the household so an agent sees the household-wide impact,
# not just the member they're currently viewing.
HOUSEHOLD_MEMBERS_OUT_OF_ORBIT = "household_members_out_of_orbit"
HOUSEHOLD_MEMBERS_OUT_OF_RANGE = "household_members_out_of_range"
HOUSEHOLD_MEMBERS_PAUSED = "household_members_paused"
# Household roll-up count of open agent follow-up tickets (household-scope).
HOUSEHOLD_OPEN_TICKETS = "household_open_tickets"

# --- Warning categories -----------------------------------------------------
# Every warning is categorized so each surface can choose what to show:
#   * SERVICE_CONFIG   -- a broken service configuration Customer Service can
#                         REMEDIATE (kitchen/cadence/case/insurance). These are
#                         the only warnings that flag a household onto the Care
#                         Management queue.
#   * MEMBER_STATE     -- an informational per-member state (paused, out of
#                         orbit/range) that is NOT fixable on Care Management.
#   * HOUSEHOLD_STATE  -- an informational household state (on hold, cancelled).
# Both informational categories still appear on the member profile header; they
# are just kept off the Care Management remediation queue. A code that is not
# mapped here defaults to informational, so a new warning can never silently
# flag members onto the queue.
SERVICE_CONFIG = "service_config"
MEMBER_STATE = "member_state"
HOUSEHOLD_STATE = "household_state"

WARNING_CATEGORY = {
    NO_KITCHEN: SERVICE_CONFIG,
    NO_CADENCE: SERVICE_CONFIG,
    CADENCE_NOT_SUPPORTED_BY_KITCHEN: SERVICE_CONFIG,
    KITCHEN_MISSING_PRODUCT: SERVICE_CONFIG,
    CADENCE_KIND_MISMATCH: SERVICE_CONFIG,
    INTERNAL_CASE_EXPIRED: SERVICE_CONFIG,
    INSURANCE_EXPIRING: SERVICE_CONFIG,
    # CareCircle cannot fix a wrong Medicaid type in the CRM (the case must be
    # closed in Unite Us), so it is informational -- NOT a Care Management queue
    # item -- but still shown on the member profile header.
    WRONG_MEDICAID_TYPE: MEMBER_STATE,
    MULTIPLE_OPEN_CASES: SERVICE_CONFIG,
    CONFLICTING_PRODUCT_TYPES: SERVICE_CONFIG,
    HOUSEHOLD_MEMBERS_OUT_OF_ORBIT: MEMBER_STATE,
    HOUSEHOLD_MEMBERS_OUT_OF_RANGE: MEMBER_STATE,
    HOUSEHOLD_MEMBERS_PAUSED: MEMBER_STATE,
    HOUSEHOLD_ON_HOLD: HOUSEHOLD_STATE,
    HOUSEHOLD_CANCELLED: HOUSEHOLD_STATE,
    HOUSEHOLD_OPEN_TICKETS: HOUSEHOLD_STATE,
}

# Allowlist of codes CS can act on -> the only warnings shown on Care Management.
CARE_MANAGEMENT_CODES = frozenset(
    code for code, category in WARNING_CATEGORY.items()
    if category == SERVICE_CONFIG
)

# Stages where a household is expected to have a kitchen + cadence: it is being
# assigned (KITCHEN_ASSIGNMENT) or already running (SERVICE_ACTIVE). These are
# the same stages the Distribution Overview counts, so an unassigned member
# surfaced there is now also surfaced on Care Management. During assignment a
# gap is expected work (ORANGE); once active a gap is broken service (RED).
_ASSIGNMENT_STAGES = (
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
)

# Meals/Boxes ProductTypeKind -> the KitchenProductType a kitchen must support.
_KIND_TO_KITCHEN_PRODUCT = {
    ProductTypeKind.MEALS: KitchenProductType.MEAL,
    ProductTypeKind.BOXES: KitchenProductType.BOX,
}


@dataclass
class Warning:
    """A single detected problem. ``client_id`` is the member it attaches to
    (the primary client for household-scope warnings). ``refs`` carries ids for
    deep-linking (case_id, insurance_id, kitchen_id, …)."""

    code: str
    severity: str          # RED | ORANGE
    scope: str             # "household" | "member"
    title: str
    detail: str = ""
    client_id: object = None
    refs: dict = field(default_factory=dict)


@dataclass
class _Context:
    enrollment: object
    client: object
    today: date
    stage: object
    product_kind: object          # ProductTypeKind | None
    cadence: str                  # household cadence code ("" when none)
    kitchen: object               # Kitchen | None
    governing_case: object        # Case | None
    open_cases: list              # open Met Council internal-service cases
    members: list                 # household member clients (incl. primary)
    has_servable_member: bool     # >=1 member is ACTIVE (being served)
    verified: bool                # household verification pop-up completed


def _coerce_kind(value):
    try:
        return ProductTypeKind(value)
    except (ValueError, TypeError):
        return None


def _case_kind(case):
    """Meals/Boxes kind for an internal-service case: the linked Program's
    ProductType if set, else the program-name keyword. None when unknown."""
    program = getattr(case, "program", None) if getattr(case, "program_id", None) else None
    if program is not None and getattr(program, "product_type_id", None):
        pt = program.product_type
        k = _coerce_kind(pt.type) if pt is not None else None
        if k is not None:
            return k
    return product_type_kind_for_name(getattr(case, "program_name", "") or "")


def _household_members(enrollment):
    """Distinct household member clients for an enrollment (incl. the primary),
    read from the enrollment's member profiles; falls back to the anchor."""
    # Normalize the de-dupe key to str(pk): a client's UUID pk can surface as a
    # UUID (DB-loaded relation) or a str (in-memory/raw FK value), and mixing the
    # two key types would let the primary slip past the de-dupe and be counted
    # (and warned about) twice.
    seen = {}
    for mp in enrollment.member_profiles.all():
        c = getattr(mp, "client", None)
        if c is not None:
            seen.setdefault(str(c.pk), c)
    anchor = enrollment.client
    if anchor is not None:
        seen.setdefault(str(anchor.pk), anchor)
    return list(seen.values())


def _has_servable_member(enrollment):
    """True when >=1 household member is being served (status ACTIVE). When every
    member is out of orbit / out of range / paused / inactive there is no
    delivery plan to configure, so a missing kitchen/cadence (or a stale
    cadence/kitchen mismatch) is expected-absent, not an actionable problem.
    Empty membership returns True so we never over-suppress on unknown data."""
    statuses = [
        mp.status for mp in enrollment.member_profiles.all()
        if mp.client_id is not None
    ]
    if not statuses:
        return True
    return any(s not in SERVICE_EXCLUDED_MEMBER_STATUSES for s in statuses)


def _build_context(enrollment):
    client = enrollment.client
    try:
        stage = EnrollmentStage(enrollment.stage)
    except ValueError:
        stage = None
    return _Context(
        enrollment=enrollment,
        client=client,
        today=timezone.localdate(),
        stage=stage,
        product_kind=product_kind_for_enrollment(enrollment),
        cadence=current_household_cadence(enrollment) or "",
        kitchen=enrollment.kitchen if enrollment.kitchen_id else None,
        governing_case=governing_internal_case(enrollment),
        # Only Met Council-managed cases live in the member base (external orgs'
        # work is excluded from the Cases tab / verification picker), so the
        # case-config warnings must count the same set -- otherwise a stray
        # non-Met case makes "multiple open cases" fire on a member who really
        # has just one. See api.services.lifecycle.case_is_met_council.
        open_cases=[
            c for c in open_internal_service_cases(client)
            if case_is_met_council(c)
        ],
        members=_household_members(enrollment),
        has_servable_member=_has_servable_member(enrollment),
        verified=verification_completed(client),
    )


# ---------------------------------------------------------------------------
# Checks. Each returns 0..N Warning objects. Keep them small and independent.
# ---------------------------------------------------------------------------
def check_multiple_open_cases(ctx):
    # Only actionable BEFORE verification: the pop-up is where the agent picks
    # the single governing case. Once the household is verified (has an active
    # enrollment tied to its case), a second open case no longer blocks anything,
    # so we don't nag Care Management about it.
    if ctx.verified:
        return []
    if len(ctx.open_cases) < 2:
        return []
    return [Warning(
        code=MULTIPLE_OPEN_CASES,
        severity=ORANGE,
        scope="household",
        title="Multiple open cases",
        detail=(
            f"{len(ctx.open_cases)} internal-service cases are open at the same "
            f"time. Review and close any that no longer apply."
        ),
        client_id=ctx.client.pk,
        refs={"case_ids": [str(c.pk) for c in ctx.open_cases]},
    )]


def check_conflicting_product_types(ctx):
    # Like multiple_open_cases, this is a PRE-verification "which case governs?"
    # nag. Once the household is verified the governing case owns the product
    # kind and any divergent open case is handled by the mismatch-reconciliation
    # flow (the Programs-tab meals<->boxes switch), so we don't keep nagging.
    if ctx.verified:
        return []
    kinds = {k for k in (_case_kind(c) for c in ctx.open_cases) if k is not None}
    if len(kinds) < 2:
        return []
    labels = ", ".join(sorted(ProductTypeKind(k).label for k in kinds))
    return [Warning(
        code=CONFLICTING_PRODUCT_TYPES,
        severity=ORANGE,
        scope="household",
        title="Conflicting product types",
        detail=f"Open cases span different product types ({labels}).",
        client_id=ctx.client.pk,
        refs={"case_ids": [str(c.pk) for c in ctx.open_cases]},
    )]


def check_no_kitchen(ctx):
    # No servable member -> the household isn't being served, so a missing
    # kitchen is expected (out-of-orbit/range members carry no delivery plan).
    if not ctx.has_servable_member:
        return []
    if ctx.stage not in _ASSIGNMENT_STAGES or ctx.kitchen is not None:
        return []
    active = ctx.stage == EnrollmentStage.SERVICE_ACTIVE
    return [Warning(
        code=NO_KITCHEN,
        severity=RED if active else ORANGE,
        scope="household",
        title="No kitchen assigned",
        detail=(
            "This household is active but has no kitchen assigned."
            if active else
            "This household is awaiting kitchen assignment."
        ),
        client_id=ctx.client.pk,
    )]


def check_no_cadence(ctx):
    # No servable member -> the household isn't being served, so a missing
    # cadence is expected (out-of-orbit/range members carry no delivery plan).
    if not ctx.has_servable_member:
        return []
    if ctx.stage not in _ASSIGNMENT_STAGES or ctx.cadence:
        return []
    active = ctx.stage == EnrollmentStage.SERVICE_ACTIVE
    return [Warning(
        code=NO_CADENCE,
        severity=RED if active else ORANGE,
        scope="household",
        title="No cadence assigned",
        detail=(
            "This household is active but has no delivery cadence set."
            if active else
            "This household is awaiting a delivery cadence."
        ),
        client_id=ctx.client.pk,
    )]


def check_cadence_not_supported_by_kitchen(ctx):
    if not ctx.has_servable_member or not ctx.cadence or ctx.kitchen is None:
        return []
    kitchen_codes = {
        c.code for c in ctx.kitchen.cadences.all() if c.is_active
    }
    if not kitchen_codes or ctx.cadence in kitchen_codes:
        return []
    return [Warning(
        code=CADENCE_NOT_SUPPORTED_BY_KITCHEN,
        severity=RED,
        scope="household",
        title="Cadence not supported by kitchen",
        detail=(
            f"The assigned cadence ({ctx.cadence}) isn't one the kitchen "
            f"'{ctx.kitchen.name}' runs."
        ),
        client_id=ctx.client.pk,
        refs={"kitchen_id": str(ctx.kitchen.pk), "cadence": ctx.cadence},
    )]


def check_kitchen_missing_product(ctx):
    if not ctx.has_servable_member or ctx.kitchen is None or ctx.product_kind is None:
        return []
    needed = _KIND_TO_KITCHEN_PRODUCT.get(ctx.product_kind)
    supported = ctx.kitchen.supported_products or []
    if needed is None or not supported or needed in supported:
        return []
    return [Warning(
        code=KITCHEN_MISSING_PRODUCT,
        severity=RED,
        scope="household",
        title="Kitchen can't make the product",
        detail=(
            f"The household's product is {ProductTypeKind(ctx.product_kind).label}, "
            f"but the assigned kitchen '{ctx.kitchen.name}' doesn't make it."
        ),
        client_id=ctx.client.pk,
        refs={
            "kitchen_id": str(ctx.kitchen.pk),
            "product_kind": ctx.product_kind.value,
        },
    )]


def check_cadence_kind_mismatch(ctx):
    if not ctx.has_servable_member or not ctx.cadence or ctx.product_kind is None:
        return []
    cadence_kinds = set(
        ProductType.objects.filter(delivery_days_cadence=ctx.cadence)
        .values_list("type", flat=True)
    )
    # Only flag when the cadence is configured for some kind(s) and the
    # household's kind is not among them (a stale setting after a switch).
    if not cadence_kinds or ctx.product_kind.value in cadence_kinds:
        return []
    return [Warning(
        code=CADENCE_KIND_MISMATCH,
        severity=RED,
        scope="household",
        title="Cadence doesn't match product type",
        detail=(
            f"The cadence ({ctx.cadence}) is set up for a different product than "
            f"the household's {ProductTypeKind(ctx.product_kind).label}."
        ),
        client_id=ctx.client.pk,
        refs={"cadence": ctx.cadence, "product_kind": ctx.product_kind.value},
    )]


def _latest_coverage_end(client):
    """The latest insurance coverage end date for a client (max expired_at),
    or None when the client has no dated insurance."""
    ends = [
        ins.expired_at for ins in client.insurances.all()
        if ins.expired_at is not None
    ]
    return max(ends) if ends else None


def _latest_active_coverage_end(client):
    """The latest end date among the client's ACTIVE insurances (max
    expired_at), or None when no active policy carries a dated end."""
    ends = [
        ins.expired_at for ins in client.insurances.all()
        if ins.status == RecordStatus.ACTIVE and ins.expired_at is not None
    ]
    return max(ends) if ends else None


def check_insurance_expiring(ctx):
    out = []
    cutoff = ctx.today + timedelta(days=INSURANCE_EXPIRING_DAYS)
    for member in ctx.members:
        # The insurance STATUS is authoritative. An ACTIVE policy means the
        # member is covered now, so never flag them as "expired" -- not even when
        # an old expired policy sits alongside the active one, and not when the
        # active policy carries a stale past end date. Only warn when that active
        # policy is genuinely about to lapse (end date within the next 30 days).
        if any(i.status == RecordStatus.ACTIVE for i in member.insurances.all()):
            end_dt = _latest_active_coverage_end(member)
            if end_dt is None:
                continue  # active with no end date => covered, nothing to flag
            end = end_dt.date() if hasattr(end_dt, "date") else end_dt
            if end < ctx.today or end > cutoff:
                continue
            out.append(Warning(
                code=INSURANCE_EXPIRING,
                severity=RED,
                scope="member",
                title="Insurance expiring",
                detail=f"Insurance expires on {end.isoformat()}.",
                client_id=member.pk,
                refs={"end_date": end.isoformat()},
            ))
            continue

        # No active policy on file: flag from the latest coverage end date.
        end_dt = _latest_coverage_end(member)
        if end_dt is None:
            continue
        end = end_dt.date() if hasattr(end_dt, "date") else end_dt
        if end > cutoff:
            continue
        expired = end < ctx.today
        out.append(Warning(
            code=INSURANCE_EXPIRING,
            severity=RED,
            scope="member",
            title="Insurance expired" if expired else "Insurance expiring",
            detail=(
                f"Insurance {'expired' if expired else 'expires'} on "
                f"{end.isoformat()}."
            ),
            client_id=member.pk,
            refs={"end_date": end.isoformat()},
        ))
    return out


def _medicaid_plans(client):
    """The client's Medicaid insurance records (plan_type == medicaid)."""
    return [
        i for i in client.insurances.all()
        if (i.plan_type or "").lower() == InsurancePlanType.MEDICAID
    ]


def member_wrong_medicaid_types(client):
    """Offending Medicaid plan names (MLTC/MAP/FFS) for ``client`` when they have
    NO clean Medicaid plan on file, else ``[]``.

    All-bad rule: a member is only flagged when EVERY named Medicaid plan is an
    ineligible type -- a single clean Medicaid plan clears it.
    """
    bad = []
    has_clean = False
    for p in _medicaid_plans(client):
        name = (p.plan_name or "").strip()
        if not name:
            continue
        if _MEDICAID_BAD_RE.search(name):
            bad.append(name)
        else:
            has_clean = True
    return [] if has_clean else bad


def check_wrong_medicaid_type(ctx):
    """Member-scope: flag members whose only Medicaid plan(s) are an
    unserviceable type (MLTC/MAP/FFS). Not fixable in the CRM -- surfaces so an
    agent can close the case in Unite Us."""
    out = []
    for member in ctx.members:
        bad = member_wrong_medicaid_types(member)
        if not bad:
            continue
        names = sorted(set(bad))
        out.append(Warning(
            code=WRONG_MEDICAID_TYPE,
            severity=RED,
            scope="member",
            title="Wrong Medicaid type",
            detail=(
                "The member's Medicaid plan type isn't served "
                f"({', '.join(_MEDICAID_INELIGIBLE_TERMS)}): {', '.join(names)}. "
                "This can't be fixed in the CRM \u2014 the case must be closed in "
                "Unite Us."
            ),
            client_id=member.pk,
            refs={"plan_names": names},
        ))
    return out


def check_internal_case_expired(ctx):
    gov = ctx.governing_case
    end_dt = getattr(gov, "service_authorization_approval_ends_at", None) if gov else None
    status = getattr(gov, "service_authorization_status", None) if gov else None
    expired = False
    end = None
    if end_dt is not None:
        end = end_dt.date() if hasattr(end_dt, "date") else end_dt
        expired = end < ctx.today
    if not expired and status != ServiceAuthorizationStatus.EXPIRED:
        return []
    detail = "The governing internal-service case's authorization has expired."
    if end is not None:
        detail = (
            f"The internal-service case authorization ended {end.isoformat()}."
        )
    return [Warning(
        code=INTERNAL_CASE_EXPIRED,
        severity=RED,
        scope="household",
        title="Internal service case expired",
        detail=detail,
        client_id=ctx.client.pk,
        refs={"case_id": str(gov.pk) if gov else None},
    )]


def check_household_on_hold(ctx):
    """Household-scope: the whole household's service is on hold."""
    if ctx.stage != EnrollmentStage.ON_HOLD:
        return []
    return [Warning(
        code=HOUSEHOLD_ON_HOLD, severity=ORANGE, scope="household",
        title="Household on hold",
        detail="Service for this household is currently on hold.",
        client_id=ctx.client.pk,
    )]


def check_household_cancelled(ctx):
    """Household-scope: the household's service has been cancelled."""
    if ctx.stage != EnrollmentStage.CANCELLED:
        return []
    return [Warning(
        code=HOUSEHOLD_CANCELLED, severity=RED, scope="household",
        title="Household cancelled",
        detail="This household's service has been cancelled.",
        client_id=ctx.client.pk,
    )]


def _plural(n, noun):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def check_household_out_of_service_counts(ctx):
    """Household-scope roll-up: how many household members are out of orbit
    (dietary/kitchen), out of range (coverage), or paused (agent hold). Each is a
    single count shown on EVERY member, so pausing / out-of-orbit / out-of-range
    for ANY household member surfaces on every member's profile (and clears for
    all of them the moment the member is unpaused / back in orbit / in range).
    Skipped for a cancelled household (terminal)."""
    if ctx.stage == EnrollmentStage.CANCELLED:
        return []
    n_orbit = n_range = n_paused = 0
    for mp in ctx.enrollment.member_profiles.all():
        if mp.client_id is None:
            continue
        if mp.status == MemberStatus.OUT_OF_ORBIT:
            n_orbit += 1
        elif mp.status == MemberStatus.OUT_OF_RANGE:
            n_range += 1
        elif mp.status == MemberStatus.PAUSED:
            n_paused += 1
    out = []
    if n_orbit:
        out.append(Warning(
            code=HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, severity=ORANGE, scope="household",
            title=f"{n_orbit} Out of Orbit",
            detail=(
                f"{_plural(n_orbit, 'household member')} can't be safely fulfilled "
                "(menu/allergy) and are excluded from deliveries and orders."
            ),
            client_id=ctx.client.pk,
            refs={"count": n_orbit},
        ))
    if n_range:
        out.append(Warning(
            code=HOUSEHOLD_MEMBERS_OUT_OF_RANGE, severity=ORANGE, scope="household",
            title=f"{n_range} Out of Range",
            detail=(
                f"{_plural(n_range, 'household member')} have an address outside the "
                "service coverage area and are excluded from deliveries and orders."
            ),
            client_id=ctx.client.pk,
            refs={"count": n_range},
        ))
    if n_paused:
        out.append(Warning(
            code=HOUSEHOLD_MEMBERS_PAUSED, severity=ORANGE, scope="household",
            title=f"{n_paused} Paused",
            detail=(
                f"{_plural(n_paused, 'household member')} are paused and excluded "
                "from deliveries and orders."
            ),
            client_id=ctx.client.pk,
            refs={"count": n_paused},
        ))
    return out


def check_household_open_tickets(ctx):
    """Household-scope roll-up: how many OPEN (Open / In Progress) agent
    follow-up tickets exist across the household's members. Shown on every
    member; clears automatically once the tickets are resolved."""
    member_ids = [m.pk for m in ctx.members if getattr(m, "pk", None) is not None]
    if not member_ids:
        return []
    n = Ticket.objects.filter(
        client_id__in=member_ids,
        status__in=(TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
    ).count()
    if not n:
        return []
    return [Warning(
        code=HOUSEHOLD_OPEN_TICKETS, severity=ORANGE, scope="household",
        title=_plural(n, "open ticket"),
        detail=(
            f"{_plural(n, 'open follow-up ticket')} for this household need agent "
            "attention."
        ),
        client_id=ctx.client.pk,
        refs={"count": n},
    )]


# Registry — add a new check here and it flows everywhere. Order is display
# order within a severity band; the UI re-sorts by severity (red first).
WARNING_CHECKS = [
    check_household_cancelled,
    check_household_on_hold,
    check_household_out_of_service_counts,
    check_household_open_tickets,
    check_no_kitchen,
    check_no_cadence,
    check_cadence_not_supported_by_kitchen,
    check_kitchen_missing_product,
    check_cadence_kind_mismatch,
    check_internal_case_expired,
    check_insurance_expiring,
    check_wrong_medicaid_type,
    check_multiple_open_cases,
    check_conflicting_product_types,
]


def evaluate_enrollment_warnings(enrollment):
    """Run every registered check against an enrollment's household and return
    the detected warnings, sorted red-first. Read-only. A failing check is
    logged and skipped so one bad rule never blocks the rest."""
    if enrollment is None:
        return []
    ctx = _build_context(enrollment)
    return _run_checks(ctx)


def _run_checks(ctx):
    out = []
    for check in WARNING_CHECKS:
        try:
            out.extend(check(ctx) or [])
        except Exception:  # pragma: no cover - defensive
            logger.exception("warning check %s failed", getattr(check, "__name__", check))
    out.sort(key=lambda w: SEVERITY_RANK.get(w.severity, 0), reverse=True)
    return out


def _household_member_ids(ctx):
    """Client ids the household's warnings can attach to (all members)."""
    return {m.pk for m in ctx.members}


# Enrollment stages that are terminal / not servable -- no warnings evaluated.
_INACTIVE_STAGES = frozenset({"disregarded", "cancelled", "closed"})


def _client_enrollments(client):
    """Servable (non-terminal) enrollment(s) governing a client's household.

    Mirrors ``active_enrollment`` but stays in the services layer (no portal
    import): the client's own enrollments, falling back to their household's
    when they're a non-primary member. Returns a list (usually one)."""
    if client is None:
        return []
    enrs = [
        e for e in client.enrollments.all()
        if e.stage not in _INACTIVE_STAGES
    ]
    if not enrs:
        membership = getattr(client, "household_membership", None)
        if membership is not None:
            enrs = [
                e for e in membership.household.enrollment_verifications.all()
                if e.stage not in _INACTIVE_STAGES
            ]
    return enrs


def sync_client_warnings(client):
    """Sync the warning snapshot for a client's household. Resolves the client's
    servable enrollment(s) and reconciles each. Best-effort: a failure is logged
    and swallowed so it can never break a case/client save or an import."""
    try:
        for enr in _client_enrollments(client):
            sync_household_warnings(enr)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "sync_client_warnings failed for client %s", getattr(client, "pk", None)
        )


def sync_household_warnings(enrollment):
    """Reconcile the persisted :class:`~api.models.MemberWarning` snapshot for a
    household against a fresh evaluation.

    Upserts every currently-detected warning (reactivating a previously resolved
    row and refreshing ``last_seen_at``), and marks any ACTIVE row that is no
    longer detected as RESOLVED (kept for history, never deleted). Rows are
    matched on the unique ``(client, code)``; the ``enrollment`` link is
    repointed to the current household so re-enrollment stays consistent.

    Returns the list of currently-active :class:`Warning` results (so callers
    like the profile view can serve them straight away). Best-effort per row.
    """
    from api.models import MemberWarning, WarningStatus

    if enrollment is None:
        return []
    ctx = _build_context(enrollment)
    detected = _run_checks(ctx)
    now = timezone.now()

    detected_keys = set()
    for w in detected:
        detected_keys.add((str(w.client_id), w.code))
        MemberWarning.objects.update_or_create(
            client_id=w.client_id, code=w.code,
            defaults={
                "enrollment": enrollment,
                "severity": w.severity,
                "scope": w.scope,
                "title": w.title,
                "detail": w.detail,
                "context": w.refs or {},
                "status": WarningStatus.ACTIVE,
                "last_seen_at": now,
                "resolved_at": None,
            },
        )

    # Resolve rows that were active for this household but are no longer
    # detected. Scope the search to the household's member clients so a member
    # who left / a fixed problem clears, without touching other households.
    active_rows = MemberWarning.objects.filter(
        client_id__in=_household_member_ids(ctx),
        status=WarningStatus.ACTIVE,
    )
    for row in active_rows:
        if (str(row.client_id), row.code) in detected_keys:
            continue
        row.status = WarningStatus.RESOLVED
        row.resolved_at = now
        row.save(update_fields=["status", "resolved_at"])

    return detected
