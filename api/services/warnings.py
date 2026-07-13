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
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

from api.models import (
    EnrollmentStage,
    KitchenProductType,
    ProductType,
    ProductTypeKind,
    ServiceAuthorizationStatus,
)
from api.services.catalog import (
    product_kind_for_enrollment,
    product_type_kind_for_name,
)
from api.services.delivery import current_household_cadence
from api.services.lifecycle import (
    governing_internal_case,
    open_internal_service_cases,
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
NO_CADENCE = "no_cadence"
CADENCE_NOT_SUPPORTED_BY_KITCHEN = "cadence_not_supported_by_kitchen"
KITCHEN_MISSING_PRODUCT = "kitchen_missing_product"
CADENCE_KIND_MISMATCH = "cadence_kind_mismatch"
INSURANCE_EXPIRING = "insurance_expiring"
INTERNAL_CASE_EXPIRED = "internal_case_expired"

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
    open_cases: list              # open internal-service cases
    members: list                 # household member clients (incl. primary)


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
    seen = {}
    for mp in enrollment.member_profiles.all():
        c = getattr(mp, "client", None)
        if c is not None and c.pk not in seen:
            seen[c.pk] = c
    if enrollment.client_id and enrollment.client_id not in seen:
        seen[enrollment.client_id] = enrollment.client
    return list(seen.values())


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
        open_cases=open_internal_service_cases(client),
        members=_household_members(enrollment),
    )


# ---------------------------------------------------------------------------
# Checks. Each returns 0..N Warning objects. Keep them small and independent.
# ---------------------------------------------------------------------------
def check_multiple_open_cases(ctx):
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


def check_no_cadence(ctx):
    if ctx.stage != EnrollmentStage.SERVICE_ACTIVE or ctx.cadence:
        return []
    return [Warning(
        code=NO_CADENCE,
        severity=RED,
        scope="household",
        title="No cadence assigned",
        detail="This household is active but has no delivery cadence set.",
        client_id=ctx.client.pk,
    )]


def check_cadence_not_supported_by_kitchen(ctx):
    if not ctx.cadence or ctx.kitchen is None:
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
    if ctx.kitchen is None or ctx.product_kind is None:
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
    if not ctx.cadence or ctx.product_kind is None:
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


def check_insurance_expiring(ctx):
    out = []
    cutoff = ctx.today + timedelta(days=INSURANCE_EXPIRING_DAYS)
    for member in ctx.members:
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


# Registry — add a new check here and it flows everywhere. Order is display
# order within a severity band; the UI re-sorts by severity (red first).
WARNING_CHECKS = [
    check_no_cadence,
    check_cadence_not_supported_by_kitchen,
    check_kitchen_missing_product,
    check_cadence_kind_mismatch,
    check_internal_case_expired,
    check_insurance_expiring,
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
