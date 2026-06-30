"""Client service-readiness diagnostic.

Produces a structured checklist explaining whether a client is ready for meal/box
service and, when not, exactly which prerequisite is missing. Read-only: it never
mutates state. Powers both the support-portal "diagnostic" panel and the
``diagnose_client`` management command.

Each check is a dict::

    {"key", "label", "status", "detail", "critical", "group"}

``status`` is one of:
    ok   - requirement satisfied
    warn - non-blocking concern (e.g. expired record, missing optional data)
    fail - blocking; a critical fail means the client is NOT ready for service
    na   - not applicable yet / no data to evaluate
"""

from django.utils import timezone

from api.models import (
    CaseType,
    ClientStage,
    EnrollmentStage,
    Insurance,
    MemberStatus,
    PurchaseOrder,
    RecordStatus,
    ServiceAuthorizationStatus,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    Ticket,
    TicketStatus,
)
from api.services.lifecycle import (
    _assessment_outcome,
    _has_met_council_screening,
    _primary_enrollment,
    derive_client_stage,
    governing_case_key,
)

OK, WARN, FAIL, NA = "ok", "warn", "fail", "na"


def _check(key, label, status, detail, group, *, critical=False):
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "group": group,
        "critical": critical,
    }


# --- individual check builders --------------------------------------------
def _insurance_check(client):
    rows = list(Insurance.objects.filter(client=client))
    if not rows:
        return _check("insurance", "Active insurance", FAIL,
                      "No insurance on file.", "coverage", critical=True)
    if any(r.status == RecordStatus.ACTIVE for r in rows):
        return _check("insurance", "Active insurance", OK,
                      f"{len(rows)} insurance record(s); at least one active.",
                      "coverage", critical=True)
    expired = any(r.status == RecordStatus.EXPIRED for r in rows)
    return _check("insurance", "Active insurance", FAIL,
                  "Insurance on file but none active"
                  + (" (latest expired)." if expired else "."),
                  "coverage", critical=True)


def _social_care_check(client):
    rows = list(SocialCareCoverage.objects.filter(client=client))
    if not rows:
        return _check("social_care_coverage", "Social care coverage", FAIL,
                      "No social care coverage on file.", "coverage", critical=True)
    if any(r.status == SocialCareCoverageStatus.ENROLLED for r in rows):
        return _check("social_care_coverage", "Social care coverage", OK,
                      "At least one enrolled coverage.", "coverage", critical=True)
    expired = any(r.status == SocialCareCoverageStatus.EXPIRED for r in rows)
    return _check("social_care_coverage", "Social care coverage", FAIL,
                  "Coverage on file but not enrolled"
                  + (" (expired)." if expired else "."),
                  "coverage", critical=True)


def _consent_check(client):
    if getattr(client, "consent_doc_url", ""):
        return _check("consent", "Consent on file", OK,
                      "Consent document present.", "coverage")
    return _check("consent", "Consent on file", WARN,
                  "No consent document URL on the client.", "coverage")


def _screening_check(client):
    if _has_met_council_screening(client):
        return _check("screening", "Met Council screening completed", OK,
                      "A completed Met Council screening exists.", "coverage")
    return _check("screening", "Met Council screening completed", WARN,
                  "No completed Met Council screening found.", "coverage")


def _assessment_check(client):
    outcome = _assessment_outcome(client)
    if outcome == "eligible":
        return _check("assessment", "Assessment eligibility", OK,
                      "Assessment outcome: eligible.", "coverage")
    if outcome == "ineligible":
        return _check("assessment", "Assessment eligibility", FAIL,
                      "Assessment outcome: ineligible.", "coverage", critical=True)
    return _check("assessment", "Assessment eligibility", NA,
                  "No resolved assessment eligibility yet.", "coverage")


def _internal_cases(client):
    return [c for c in client.cases.all() if c.case_type == CaseType.INTERNAL_SERVICE]


def _case_check(client):
    cases = _internal_cases(client)
    if not cases:
        return _check("internal_service_case", "Internal-service case", FAIL,
                      "No internal-service (meals/box) case.", "case", critical=True)
    gov = max(cases, key=governing_case_key)
    status = gov.service_authorization_status or "(blank)"
    label = gov.service_authorization_status_label or status
    if gov.service_authorization_status in (
        ServiceAuthorizationStatus.APPROVED, ServiceAuthorizationStatus.NOT_REQUIRED
    ):
        st = OK
    elif gov.service_authorization_status == ServiceAuthorizationStatus.PENDING:
        st = WARN
    else:  # denied / expired / blank
        st = FAIL
    return _check("internal_service_case", "Internal-service case authorized", st,
                  f"{len(cases)} internal case(s); governing {gov.case_id} "
                  f"auth={label}.", "case", critical=True)


def _auth_window_check(client):
    cases = _internal_cases(client)
    if not cases:
        return _check("auth_window", "Authorization window valid", NA,
                      "No internal-service case to evaluate.", "case")
    gov = max(cases, key=governing_case_key)
    ends = gov.service_authorization_approval_ends_at
    if ends is None:
        return _check("auth_window", "Authorization window valid", NA,
                      "No approval end date on the governing case.", "case")
    if ends < timezone.now():
        return _check("auth_window", "Authorization window valid", FAIL,
                      f"Authorization window ended {ends:%Y-%m-%d}.", "case",
                      critical=True)
    return _check("auth_window", "Authorization window valid", OK,
                  f"Authorized through {ends:%Y-%m-%d}.", "case")


def _lifecycle_check(client):
    stored = str(client.lifecycle_stage)
    derived = str(derive_client_stage(client))
    if stored == derived:
        return _check("lifecycle_stage", "Lifecycle stage up to date", OK,
                      f"stored == derived == {stored}.", "lifecycle")
    return _check("lifecycle_stage", "Lifecycle stage up to date", WARN,
                  f"STALE: stored={stored} but derived={derived}. "
                  "Run recompute_client_stage.", "lifecycle")


def _active_status_check(client, enr):
    """"Active in ARM": treated as lifecycle_stage == active OR enrollment in
    Service Active. Adjust if ARM is a separate system."""
    is_active = client.lifecycle_stage == ClientStage.ACTIVE or (
        enr is not None and EnrollmentStage(enr.stage) == EnrollmentStage.SERVICE_ACTIVE
    )
    if is_active:
        return _check("active_status", "Active (in service)", OK,
                      f"lifecycle_stage={client.lifecycle_stage}.", "lifecycle")
    return _check("active_status", "Active (in service)", WARN,
                  f"Not yet active (lifecycle_stage={client.lifecycle_stage}).",
                  "lifecycle")


def _enrollment_check(enr):
    if enr is None:
        return _check("enrollment", "Enrollment exists", WARN,
                      "No verification enrollment for this client/household.",
                      "lifecycle")
    return _check("enrollment", "Enrollment exists", OK,
                  f"Enrollment {enr.pk} stage={enr.stage}.", "lifecycle")


def _member_profiles(enr):
    if enr is None:
        return []
    return list(enr.member_profiles.select_related("client").all())


def _dietary_check(profiles):
    if not profiles:
        return _check("dietary_restrictions", "Dietary restrictions captured", NA,
                      "No member profiles yet.", "verification")
    answered = sum(
        1 for p in profiles
        if p.dietary_restrictions or p.food_allergies or p.other_dietary_restrictions
    )
    return _check("dietary_restrictions", "Dietary restrictions captured", OK,
                  f"{answered}/{len(profiles)} member(s) have dietary data "
                  "(empty is valid = no restrictions).", "verification")


def _menu_type_check(profiles):
    if not profiles:
        return _check("menu_type", "Menu type assigned", NA,
                      "No member profiles yet.", "verification", critical=True)
    active = [p for p in profiles if p.status != MemberStatus.OUT_OF_ORBIT]
    missing = [p for p in active if not p.menu_type]
    if missing:
        names = ", ".join(p.member_name or str(p.client_id) for p in missing)
        return _check("menu_type", "Menu type assigned", FAIL,
                      f"{len(missing)} active member(s) missing menu type: {names}.",
                      "verification", critical=True)
    return _check("menu_type", "Menu type assigned", OK,
                  f"All {len(active)} active member(s) have a menu type.",
                  "verification", critical=True)


def _quantity_check(profiles):
    if not profiles:
        return _check("meals_per_delivery", "Meals/boxes per delivery set", NA,
                      "No member profiles yet.", "verification")
    active = [p for p in profiles if p.status != MemberStatus.OUT_OF_ORBIT]
    missing = [p for p in active if not p.meals_per_delivery]
    if missing:
        return _check("meals_per_delivery", "Meals/boxes per delivery set", WARN,
                      f"{len(missing)} active member(s) missing quantity.",
                      "verification")
    return _check("meals_per_delivery", "Meals/boxes per delivery set", OK,
                  "All active members have a per-delivery quantity.", "verification")


def _out_of_orbit_check(profiles):
    out = [p for p in profiles if p.status == MemberStatus.OUT_OF_ORBIT]
    if not out:
        return _check("out_of_orbit", "No out-of-orbit members", OK,
                      "All members fulfillable.", "verification")
    names = ", ".join(p.member_name or str(p.client_id) for p in out)
    return _check("out_of_orbit", "No out-of-orbit members", WARN,
                  f"{len(out)} member(s) out of orbit (excluded from delivery): "
                  f"{names}.", "verification")


def _verified_flags_check(enr):
    if enr is None:
        return _check("verified_flags", "Family/Medicaid verified", NA,
                      "No enrollment yet.", "verification")
    missing = []
    if not enr.is_family_verified:
        missing.append("family")
    if not enr.medicaid_type_verified:
        missing.append("medicaid type")
    if missing:
        return _check("verified_flags", "Family/Medicaid verified", WARN,
                      "Not verified: " + ", ".join(missing) + ".", "verification")
    return _check("verified_flags", "Family/Medicaid verified", OK,
                  "Family and Medicaid type verified.", "verification")


def _kitchen_check(enr):
    if enr is None:
        return _check("kitchen", "Kitchen assigned", NA,
                      "No enrollment yet.", "logistics", critical=True)
    if enr.kitchen_id:
        return _check("kitchen", "Kitchen assigned", OK,
                      f"Kitchen: {enr.kitchen.name}.", "logistics", critical=True)
    return _check("kitchen", "Kitchen assigned", FAIL,
                  "No kitchen assigned.", "logistics", critical=True)


def _address_check(enr):
    if enr is None:
        return _check("delivery_address", "Delivery address present & verified", NA,
                      "No enrollment yet.", "logistics", critical=True)
    if not enr.delivery_address_id:
        return _check("delivery_address", "Delivery address present & verified", FAIL,
                      "No delivery address on the enrollment.", "logistics",
                      critical=True)
    if not enr.delivery_address_verified:
        return _check("delivery_address", "Delivery address present & verified", WARN,
                      "Delivery address present but not verified.", "logistics")
    return _check("delivery_address", "Delivery address present & verified", OK,
                  "Delivery address present and verified.", "logistics", critical=True)


def _weekdays_check(enr):
    if enr is None:
        return _check("delivery_weekdays", "Delivery cadence set", NA,
                      "No enrollment yet.", "logistics")
    if enr.delivery_weekdays:
        return _check("delivery_weekdays", "Delivery cadence set", OK,
                      f"Weekdays: {', '.join(enr.delivery_weekdays)}.", "logistics")
    return _check("delivery_weekdays", "Delivery cadence set", WARN,
                  "No delivery weekdays selected.", "logistics")


def _orders_check(client, enr):
    sched_count = enr.orders.count() if enr is not None else 0
    po_count = (
        PurchaseOrder.objects.filter(delivery_orders__member_id=client.pk)
        .distinct().count()
    )
    if sched_count == 0 and po_count == 0:
        return _check("orders", "Delivery orders generated", WARN,
                      "No scheduled orders or purchase orders yet.", "logistics")
    return _check("orders", "Delivery orders generated", OK,
                  f"{sched_count} scheduled order(s); {po_count} purchase order(s).",
                  "logistics")


def _tickets_check(client):
    open_count = Ticket.objects.filter(client=client).exclude(
        status=TicketStatus.RESOLVED
    ).count()
    if open_count == 0:
        return _check("open_tickets", "No open follow-up tickets", OK,
                      "No unresolved tickets.", "tickets")
    return _check("open_tickets", "No open follow-up tickets", WARN,
                  f"{open_count} unresolved ticket(s) need attention.", "tickets")


# --- public API ------------------------------------------------------------
def diagnose_client(client):
    """Return the full readiness diagnostic for ``client`` (read-only)."""
    enr = _primary_enrollment(client)
    profiles = _member_profiles(enr)

    checks = [
        _insurance_check(client),
        _social_care_check(client),
        _consent_check(client),
        _screening_check(client),
        _assessment_check(client),
        _case_check(client),
        _auth_window_check(client),
        _lifecycle_check(client),
        _enrollment_check(enr),
        _active_status_check(client, enr),
        _dietary_check(profiles),
        _menu_type_check(profiles),
        _quantity_check(profiles),
        _out_of_orbit_check(profiles),
        _verified_flags_check(enr),
        _kitchen_check(enr),
        _address_check(enr),
        _weekdays_check(enr),
        _orders_check(client, enr),
        _tickets_check(client),
    ]

    # Ready for service requires EVERY critical prerequisite to be satisfied
    # (ok). A critical check that is fail/warn/na (e.g. no kitchen yet, address
    # unverified, no internal case) blocks readiness.
    blocking = [c for c in checks if c["critical"] and c["status"] != OK]
    summary = {
        "ok": sum(1 for c in checks if c["status"] == OK),
        "warn": sum(1 for c in checks if c["status"] == WARN),
        "fail": sum(1 for c in checks if c["status"] == FAIL),
        "na": sum(1 for c in checks if c["status"] == NA),
    }

    return {
        "client_id": str(client.pk),
        "name": f"{client.first_name} {client.last_name}".strip(),
        "lifecycle_stage": str(client.lifecycle_stage),
        "ready_for_service": not blocking,
        "blocking": [c["key"] for c in blocking],
        "summary": summary,
        "checks": checks,
    }
