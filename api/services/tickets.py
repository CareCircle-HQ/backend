"""Agent follow-up ticket rules (spec §6).

``open_ticket`` is idempotent per (type, client, case): if an open/in-progress
ticket of the same type already exists for the same subject it is reused (its
reason + import_run refreshed) rather than duplicated, so daily re-runs don't
pile up tickets. The ``evaluate_*`` helpers encode the conditions and are called
by the import orchestration after each entity is upserted.
"""

import logging
from dataclasses import dataclass

from api.history import ChangeSource
from api.models import (
    CaseStatus,
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
                client=None, case=None, import_run=None, source="", actor=""):
    """Create (or refresh) an open ticket of ``ticket_type`` for this subject.

    ``ticket_type`` may be a :class:`TicketType` instance or a code string
    (e.g. ``TicketTypeCode.CASE_CLOSED``).

    Returns (ticket, created). Idempotent: an existing open/in-progress ticket of
    the same type for the same (client, case) is reused. On a NEW ticket a
    'New Ticket Created' timeline event is emitted (attributed to ``source`` /
    ``actor``) so every ticket -- from the import, the daily sync, or a live
    extension write -- lands on the client's history.
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
    # Mirror the new ticket onto the client's timeline (best-effort: a timeline
    # hiccup must never fail the ticket write). Deduped on the ticket pk.
    if client is not None:
        try:
            from api.services import timeline

            timeline.event_for_ticket_created(
                ticket, source=source or ChangeSource.SYSTEM, actor=actor,
            )
        except Exception:  # noqa: BLE001
            logger.warning("open_ticket timeline emit failed", exc_info=True)
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


@dataclass
class PlannedTicket:
    """A ticket a rule WOULD open. Lets callers preview the work an import will
    generate (report mode) before committing to creating it, and lets both the
    preview and the real path share one source of truth for the conditions."""

    type_code: str
    reason: str
    action: str  # machine label for aggregation, e.g. "case_closed"
    severity: str = TicketSeverity.MEDIUM
    client: object = None
    case: object = None
    # For the auth-changed action: the new authorization status (for breakdowns).
    detail: str = ""

    def open(self, import_run=None, source="", actor=""):
        return open_ticket(
            self.type_code, reason=self.reason, severity=self.severity,
            client=self.client, case=self.case, import_run=import_run,
            source=source, actor=actor,
        )


def plan_case_tickets(case, *, previous_status=None, previous_auth_status=None):
    """The tickets :func:`evaluate_case` would open for this case, as a list of
    :class:`PlannedTicket` (no DB writes). Case closed, authorization changed,
    and case-with-no-services (spec §6)."""
    plans = []
    if case.case_status == CaseStatus.CLOSED and previous_status != CaseStatus.CLOSED:
        plans.append(PlannedTicket(
            type_code=TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            action="cases_closed",
            reason=(
                f"Case {case.case_id} changed to Closed in Unite Us. Review whether "
                f"the member's meal/box service should be paused or closed, and "
                f"follow up with the member to confirm the end of service."
            ),
            client=case.client, case=case,
        ))

    if (
        previous_auth_status is not None
        and case.service_authorization_status
        and case.service_authorization_status != previous_auth_status
    ):
        plans.append(PlannedTicket(
            type_code=TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            action="auth_changed",
            detail=case.service_authorization_status,
            reason=(
                f"Service authorization for case {case.case_id} changed from "
                f"'{previous_auth_status or '-'}' to "
                f"'{case.service_authorization_status}'. Review the new "
                f"authorization and adjust the member's service (activate, pause, "
                f"or close) accordingly."
            ),
            client=case.client, case=case,
        ))
    # NOTE: a 'case has no contracted services' rule was intentionally removed --
    # only the household primary holds internal-service (meal/box) cases, so it
    # fired for essentially every member case and flooded the queue/timeline.
    return plans


def evaluate_case(case, *, previous_status=None, previous_auth_status=None,
                  import_run=None):
    """Open the tickets planned for this case (spec §6)."""
    for plan in plan_case_tickets(
        case, previous_status=previous_status,
        previous_auth_status=previous_auth_status,
    ):
        plan.open(import_run=import_run)


# NOTE: an expired/unreadable Unite Us credential is an integration problem
# (an agent must re-login via the extension), NOT customer-support work, so it
# intentionally does NOT open a work-queue ticket. The daily pull logs it and
# records it on the ImportRun's errors instead (see services/uniteus_import.py).
