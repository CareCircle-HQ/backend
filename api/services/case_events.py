"""Shared case-change tracking.

One place that, for a case whose status/authorization may have changed, both
(a) records the change on the client's timeline and (b) opens the follow-up
tickets it triggers -- so tracking + attribution are identical no matter which
write path the change arrived through:

- CSV import            (api.services.csv_import)
- daily Unite Us sync   (api.services.uniteus_import)
- live extension write  (api.views.CaseViewSet / CaseSerializer)

Callers pass their own ``source`` (import|extension|admin|crm|system) and
``actor`` (e.g. ``agent:355`` or ``system:unite-us-import``) so every emitted
event/ticket says WHO did it and via WHICH channel. The ticket-created timeline
event is emitted inside :func:`api.services.tickets.open_ticket`, so this module
never double-writes it.
"""

import logging
from dataclasses import dataclass, field

from api.history import ChangeSource
from api.services import tickets, timeline

logger = logging.getLogger(__name__)


@dataclass
class CaseChangeResult:
    """What :func:`record_case_change` did, for callers that aggregate (e.g. the
    import's Import Activity counters)."""

    status_changed: bool = False
    auth_changed: bool = False
    new_auth: str = ""
    timeline_events: int = 0
    tickets_created: int = 0
    # One entry per detected follow-up ticket: {action, detail, reason, created}.
    planned: list = field(default_factory=list)


def record_case_change(
    case,
    *,
    previous_status=None,
    previous_auth=None,
    source=ChangeSource.SYSTEM,
    actor="",
    create_tickets=False,
    emit_timeline=True,
    skip_actions=frozenset(),
    import_run=None,
):
    """Emit case status/authorization-change timeline events and open the
    follow-up tickets the change triggers.

    ``previous_status`` / ``previous_auth`` are the pre-save values (None for a
    brand-new case -> no change events, but ticket rules still evaluate). Tickets
    are opened only when ``create_tickets`` is True and the action isn't in
    ``skip_actions`` (e.g. ``case_no_services`` for CSV imports); detection is
    always recorded in the returned result for review. Best-effort throughout:
    a tracking hiccup never propagates to the caller's write.
    """
    result = CaseChangeResult()

    # 1) Case status transition (any -> Closed/Managed/Cancelled/...). Detection
    #    is independent of ``emit_timeline`` so a caller that suppresses the
    #    timeline (imports / cron -> Care Management is the source of truth) still
    #    gets an accurate change summary in the result.
    if (
        previous_status is not None
        and case.case_status
        and case.case_status != previous_status
    ):
        result.status_changed = True
        if emit_timeline:
            try:
                ev = timeline.event_for_case_status_change(
                    case, previous_status=previous_status, source=source,
                    actor=actor, import_run=import_run,
                )
                if ev is not None:
                    result.timeline_events += 1
            except Exception:  # noqa: BLE001
                logger.warning("record_case_change: status event failed", exc_info=True)

    # 2) Service-authorization transition (approved/denied/pending/expired).
    if (
        previous_auth is not None
        and case.service_authorization_status
        and case.service_authorization_status != previous_auth
    ):
        result.auth_changed = True
        result.new_auth = case.service_authorization_status
        if emit_timeline:
            try:
                ev = timeline.event_for_case_authorization_change(
                    case, previous_auth=previous_auth, source=source,
                    actor=actor, import_run=import_run,
                )
                if ev is not None:
                    result.timeline_events += 1
            except Exception:  # noqa: BLE001
                logger.warning("record_case_change: auth event failed", exc_info=True)

    # 3) Follow-up tickets. Detect always (so callers can preview); open only
    #    when enabled + not excluded. open_ticket emits its own timeline event.
    try:
        plans = tickets.plan_case_tickets(
            case, previous_status=previous_status,
            previous_auth_status=previous_auth,
        )
    except Exception:  # noqa: BLE001
        logger.warning("record_case_change: plan_case_tickets failed", exc_info=True)
        plans = []

    for plan in plans:
        created = create_tickets and plan.action not in skip_actions
        if created:
            try:
                plan.open(import_run=import_run, source=source, actor=actor)
                result.tickets_created += 1
            except Exception:  # noqa: BLE001
                logger.warning("record_case_change: open ticket failed", exc_info=True)
                created = False
        result.planned.append({
            "action": plan.action,
            "detail": plan.detail,
            "reason": plan.reason,
            "created": created,
        })

    return result
