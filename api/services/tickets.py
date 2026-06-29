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
    # Dedupe on (type, client, case, reason): now that the import routes every
    # detection through SYSTEM_CHANGE_DETECTED, the reason is what distinguishes
    # one detected change from another for the same subject.
    existing = Ticket.objects.filter(
        type=type_obj, status__in=OPEN_STATUSES, client=client, case=case,
        reason=reason,
    ).first()
    if existing:
        if import_run and existing.import_run_id != import_run.pk:
            existing.import_run = import_run
            existing.save(update_fields=["import_run", "updated_at"])
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
        open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            reason=(
                "Member has no active insurance on file"
                + (" (their previous insurance has expired)" if has_expired_ins else "")
                + ". Confirm the member's current insurance with them and update the "
                "record before making any service eligibility decisions."
            ),
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
        open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            reason=(
                "Member has no active social care coverage"
                + (" (their coverage has expired)" if has_expired_cov else "")
                + ". Verify the member's social care enrollment is current; without "
                "active coverage the member may not be eligible for service."
            ),
            client=client, import_run=import_run,
        )


def evaluate_new_insurance(client, import_run=None):
    open_ticket(
        TicketTypeCode.SYSTEM_CHANGE_DETECTED,
        reason=(
            "A new insurance record was created for this member from the latest "
            "import. Review the plan type, member ID and status, and confirm the "
            "details are correct before relying on them for eligibility."
        ),
        client=client, import_run=import_run,
    )


def evaluate_new_coverage(client, import_run=None):
    open_ticket(
        TicketTypeCode.SYSTEM_CHANGE_DETECTED,
        reason=(
            "A new social care coverage record was created for this member from "
            "the latest import. Confirm the coverage program, status and effective "
            "dates are correct."
        ),
        client=client, import_run=import_run,
    )


def evaluate_member_not_found(reference, import_run=None):
    open_ticket(
        TicketTypeCode.SYSTEM_CHANGE_DETECTED,
        reason=(
            f"An incoming import record references a member that does not exist in "
            f"the system: {reference}. Locate the matching member (or create one) "
            f"and re-link the record so the import can process it."
        ),
        severity=TicketSeverity.HIGH, import_run=import_run,
    )


def evaluate_case(case, *, previous_status=None, previous_auth_status=None,
                  import_run=None):
    """Case closed, authorization changed, and case-with-no-services (spec §6)."""
    if case.case_status == CaseStatus.CLOSED and previous_status != CaseStatus.CLOSED:
        open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            reason=(
                f"Case {case.case_id} changed to Closed in Unite Us. Review whether "
                f"the member's meal/box service should be paused or closed, and "
                f"follow up with the member to confirm the end of service."
            ),
            client=case.client, case=case, import_run=import_run,
        )

    if (
        previous_auth_status is not None
        and case.service_authorization_status
        and case.service_authorization_status != previous_auth_status
    ):
        open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            reason=(
                f"Service authorization for case {case.case_id} changed from "
                f"'{previous_auth_status or '-'}' to "
                f"'{case.service_authorization_status}'. Review the new "
                f"authorization and adjust the member's service (activate, pause, "
                f"or close) accordingly."
            ),
            client=case.client, case=case, import_run=import_run,
        )

    if not ContractedService.objects.filter(case=case).exists():
        open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            reason=(
                f"Case {case.case_id} has no contracted (internal) services "
                f"attached, so the member has no active internal-services "
                f"contract. Confirm whether an internal-services contract needs to "
                f"be added before meal/box service can proceed."
            ),
            client=case.client, case=case, import_run=import_run,
        )


def evaluate_credential_expired(credential, import_run=None):
    open_ticket(
        TicketTypeCode.SYSTEM_CHANGE_DETECTED,
        reason=(
            f"The Unite Us login expired for provider {credential.provider_id}"
            + (f" / employee {credential.employee_id}" if credential.employee_id else "")
            + ". An agent must re-login to Unite Us so the daily data pull can "
            "resume; until then member data will not refresh."
        ),
        severity=TicketSeverity.HIGH, import_run=import_run,
    )
