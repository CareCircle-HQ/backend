"""Close SUPERSEDED enrollments that were left non-terminal.

A governing-case replacement supersedes the old enrollment and should CLOSE it,
but a swallowed ``InvalidTransition`` previously left some superseded rows LIVE
(still at verified / kitchen_assignment / service_active). That produced TWO live
enrollments for one client -- inflating the Distribution matrix, double-feeding
POs, and letting the profile / nutritionist flows read the stale one.

This closes each superseded-but-non-terminal enrollment, first carrying its
Nutritionist sign-off onto the surviving (superseding) enrollment, then
recomputing the client's lifecycle stage.

SAFETY: it will NOT close a superseded row that still has a live (future)
delivery schedule when its SURVIVOR has none -- closing that would end the
member's service. Those are reported for manual review instead.

Dry-run by default; pass --apply to commit. Use --limit N to process a small
batch first, and --list for per-enrollment detail.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    CaseStatus,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    MemberDeliverySchedule,
    ScheduleStatus,
)
from api.services.lifecycle import recompute_client_stage

_TERMINAL = [EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED]
_CLOSED_CASE_STATUSES = {CaseStatus.CLOSED, CaseStatus.CANCELLED}

_NUTRI_FIELDS = [
    "nutritionist_approved_at", "nutritionist_approved_by",
    "nutritionist_signature", "nutritionist_signature_image",
    "nutritionist_approval_pdf_key",
]


def _future_sched(enr, today):
    """Count of live (SCHEDULED, not-yet-ended) delivery schedules on ``enr`` --
    a proxy for 'this enrollment is actively serving'."""
    if enr is None:
        return 0
    return (
        MemberDeliverySchedule.objects
        .filter(enrollment=enr, status=ScheduleStatus.SCHEDULED)
        .exclude(ends_on__lt=today)
        .count()
    )


class Command(BaseCommand):
    help = (
        "Close superseded-but-live enrollments (carrying nutritionist approval to "
        "the survivor), skipping any where closing would end active service. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the changes.")
        parser.add_argument("--list", action="store_true", help="Print per-enrollment detail.")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N enrollments this run (0 = no limit).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        list_detail = opts["list"]
        limit = opts["limit"]
        today = timezone.localdate()

        qs = (
            EnrollmentVerification.objects
            .filter(superseded_by__isnull=False)
            .exclude(stage__in=[s.value for s in _TERMINAL])
            .select_related("client")
            .distinct()
            .order_by("pk")
        )

        to_close, unsafe, open_case = [], [], []
        for old in qs:
            survivor = old.superseded_by.first()
            old_future = _future_sched(old, today)
            surv_future = _future_sched(survivor, today)
            # DANGER 1: the superseded row is the one actually serving and the
            # survivor is not -> closing it would drop the member's only live
            # plan. Leave it for manual review.
            if old_future > 0 and surv_future == 0:
                unsafe.append((old, survivor, old_future, surv_future))
                continue
            # DANGER 2: the superseded row is bound to a DISTINCT, still-OPEN
            # internal-service case that no live enrollment covers -> closing it
            # would ORPHAN that open case (an open program with no enrollment).
            # That's a separate program, not a pure duplicate, so leave it.
            oc = getattr(old, "case", None)
            if (
                oc is not None
                and oc.case_status not in _CLOSED_CASE_STATUSES
                and (survivor is None or survivor.case_id != old.case_id)
                and not EnrollmentVerification.objects
                    .filter(case_id=old.case_id)
                    .exclude(pk=old.pk)
                    .exclude(stage__in=[s.value for s in _TERMINAL])
                    .exists()
            ):
                open_case.append((old, survivor))
                continue
            to_close.append((old, survivor, old_future, surv_future))

        if limit and len(to_close) > limit:
            to_close = to_close[:limit]

        by_stage = Counter(o.stage for o, *_ in to_close)
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n=== Close superseded-but-live enrollments ==="
        ))
        self.stdout.write(f"  safe to close: {len(to_close)}"
                          + (f"  (limited to {limit})" if limit else ""))
        for stage, n in sorted(by_stage.items()):
            self.stdout.write(f"     {n:6}  {stage}")
        if unsafe:
            self.stdout.write(self.style.WARNING(
                f"  SKIPPED (superseded row is the live server; survivor has no "
                f"schedule) -- review manually: {len(unsafe)}"
            ))
            for old, surv, of, sf in unsafe[:50]:
                self.stdout.write(
                    f"     old={old.pk} ({old.stage}, future={of}) "
                    f"survivor={surv.pk if surv else None} (future={sf}) client={old.client_id}"
                )
        if open_case:
            self.stdout.write(self.style.WARNING(
                f"  SKIPPED (superseded row holds a DISTINCT open case -> would "
                f"orphan that open program): {len(open_case)}"
            ))
            for old, surv in open_case[:50]:
                self.stdout.write(
                    f"     old={old.pk} case={old.case_id} (open) "
                    f"survivor={surv.pk if surv else None} client={old.client_id}"
                )

        if list_detail:
            for old, surv, of, sf in to_close:
                c = old.client
                name = (f"{c.first_name} {c.last_name}".strip() if c else "") or "?"
                self.stdout.write(
                    f"    close old={old.pk} {name:22.22} {old.stage:16.16} "
                    f"future={of}  ->  survivor={surv.pk if surv else None} future={sf}"
                )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing changed. Re-run with --apply."
            ))
            return

        closed = 0
        client_ids = set()
        now = timezone.now()
        for i in range(0, len(to_close), 200):
            batch = to_close[i:i + 200]
            with transaction.atomic():
                for old, survivor, _of, _sf in batch:
                    if (
                        survivor is not None
                        and old.nutritionist_approved_at
                        and not survivor.nutritionist_approved_at
                    ):
                        for f in _NUTRI_FIELDS:
                            setattr(survivor, f, getattr(old, f))
                        survivor.save(update_fields=_NUTRI_FIELDS)
                    old.stage = EnrollmentStage.CLOSED
                    old.stage_at = now
                    if old.closed_at is None:
                        old.closed_at = now
                    old.close_reason = old.close_reason or "duplicate_superseded"
                    old.save(update_fields=["stage", "stage_at", "closed_at", "close_reason"])
                    # Drop the stale row's future deliveries so it stops feeding POs
                    # / the distribution matrix (batched/committed rows preserved).
                    try:
                        from api.services.orders import truncate_future_deliveries
                        truncate_future_deliveries(old)
                    except Exception:  # noqa: BLE001
                        pass
                    closed += 1
                    if old.client_id:
                        client_ids.add(old.client_id)

        healed = 0
        for cid in client_ids:
            c = Client.objects.filter(pk=cid).first()
            if c is None:
                continue
            try:
                recompute_client_stage(c)
                healed += 1
            except Exception:  # noqa: BLE001
                self.stderr.write(f"  recompute failed for client {cid}")

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: closed {closed} stale enrollment(s); recomputed {healed} "
            f"client(s). Skipped -- serving: {len(unsafe)}, open-case: {len(open_case)}."
        ))
