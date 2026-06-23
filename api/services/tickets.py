"""Agent follow-up ticket rules (spec §6).

``open_ticket`` is idempotent per (type, client, case): if an open/in-progress
ticket of the same type already exists for the same subject it is reused (its
reason + import_run refreshed) rather than duplicated, so daily re-runs don't
pile up tickets. The ``evaluate_*`` helpers encode the conditions and are called
by the import orchestration after each entity is upserted.
"""

import logging

from api.models import (
    CaseStatus,
    ContractedService,
    Insurance,
    RecordStatus,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    Ticket,
    TicketStatus,
    TicketSeverity,
    TicketType,
    TicketTypeCode,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)


def _resolve_ticket_type(ticket_type):
    """Accept either a :class:`TicketType` instance or a code string and return
    the matching :class:`TicketType` row, creating it on the fly if the code
    isn't seeded yet (keeps the daily pull resilient to new codes)."""
    if isinstance(ticket_type, TicketType):
        return ticket_type
    code = str(ticket_type)
    label = TicketTypeCode(code).label if code in TicketTypeCode.values else code
    obj, _ = TicketType.objects.get_or_create(code=code, defaults={"label": label})
    return obj


def open_ticket(ticket_type, *, reason, severity=TicketSeverity.MEDIUM,
                client=None, case=None, import_run=None):
    """Create (or refresh) an open ticket of ``ticket_type`` for this subject.

    ``ticket_type`` may be a :class:`TicketType` instance or a code string
    (e.g. ``TicketTypeCode.CASE_CLOSED``).

    Returns (ticket, created). Idempotent: an existing open/in-progress ticket of
    the same type for the same (client, case) is reused.
    """
    type_obj = _resolve_ticket_type(ticket_type)
    existing = Ticket.objects.filter(
        type=type_obj, status__in=OPEN_STATUSES, client=client, case=case
    ).first()
    if existing:
        changed = []
        if reason and existing.reason != reason:
            existing.reason = reason
            changed.append("reason")
        if import_run and existing.import_run_id != import_run.pk:
            existing.import_run = import_run
            changed.append("import_run")
        if changed:
            existing.save(update_fields=changed + ["updated_at"])
        return existing, False
    ticket = Ticket.objects.create(
        type=type_obj, reason=reason, severity=severity,
        client=client, case=case, import_run=import_run,
    )
    return ticket, True


# --- rule evaluators -------------------------------------------------------
def evaluate_client_coverage(client, import_run=None):
    """No/expired insurance and no/expired social care coverage (spec §6)."""
    insurances = list(Insurance.objects.filter(client=client))
    has_active_ins = any(i.status == RecordStatus.ACTIVE for i in insurances)
    has_expired_ins = any(i.status == RecordStatus.EXPIRED for i in insurances)
    if not has_active_ins:
        ttype = (
            TicketTypeCode.INSURANCE_EXPIRED if has_expired_ins
            else TicketTypeCode.NO_ACTIVE_INSURANCE
        )
        open_ticket(
            ttype,
            reason="Member has no active insurance"
            + (" (existing insurance expired)." if has_expired_ins else "."),
            client=client, import_run=import_run,
        )

    coverages = list(SocialCareCoverage.objects.filter(client=client))
    has_active_cov = any(
        c.status == SocialCareCoverageStatus.ENROLLED for c in coverages
    )
    has_expired_cov = any(
        c.status == SocialCareCoverageStatus.EXPIRED for c in coverages
    )
    if not has_active_cov:
        ttype = (
            TicketTypeCode.COVERAGE_EXPIRED if has_expired_cov
            else TicketTypeCode.NO_ACTIVE_COVERAGE
        )
        open_ticket(
            ttype,
            reason="Member has no active social care coverage"
            + (" (coverage expired)." if has_expired_cov else "."),
            client=client, import_run=import_run,
        )


def evaluate_new_insurance(client, import_run=None):
    open_ticket(
        TicketTypeCode.NEW_INSURANCE,
        reason="New insurance created from the import; requires agent validation.",
        client=client, import_run=import_run,
    )


def evaluate_new_coverage(client, import_run=None):
    open_ticket(
        TicketTypeCode.NEW_COVERAGE,
        reason="New social care coverage created from the import.",
        client=client, import_run=import_run,
    )


def evaluate_member_not_found(reference, import_run=None):
    open_ticket(
        TicketTypeCode.MEMBER_NOT_FOUND,
        reason=f"Incoming record references an unknown member: {reference}.",
        severity=TicketSeverity.HIGH, import_run=import_run,
    )


def evaluate_case(case, *, previous_status=None, previous_auth_status=None,
                  import_run=None):
    """Case closed, authorization changed, and case-with-no-services (spec §6)."""
    if case.case_status == CaseStatus.CLOSED and previous_status != CaseStatus.CLOSED:
        open_ticket(
            TicketTypeCode.CASE_CLOSED,
            reason=f"Case {case.case_id} changed to Closed.",
            client=case.client, case=case, import_run=import_run,
        )

    if (
        previous_auth_status is not None
        and case.service_authorization_status
        and case.service_authorization_status != previous_auth_status
    ):
        open_ticket(
            TicketTypeCode.AUTHORIZATION_CHANGED,
            reason=(
                f"Authorization status changed: "
                f"{previous_auth_status or '-'} -> {case.service_authorization_status}."
            ),
            client=case.client, case=case, import_run=import_run,
        )

    if not ContractedService.objects.filter(case=case).exists():
        open_ticket(
            TicketTypeCode.CASE_NO_SERVICES,
            reason=f"Case {case.case_id} has no contracted services.",
            client=case.client, case=case, import_run=import_run,
        )


def evaluate_credential_expired(credential, import_run=None):
    open_ticket(
        TicketTypeCode.CREDENTIAL_EXPIRED,
        reason=(
            f"Unite Us login expired for provider {credential.provider_id}"
            + (f" / employee {credential.employee_id}" if credential.employee_id else "")
            + "; agent must re-login so the daily pull can resume."
        ),
        severity=TicketSeverity.HIGH, import_run=import_run,
    )
