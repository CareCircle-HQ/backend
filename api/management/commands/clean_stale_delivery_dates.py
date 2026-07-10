"""Find and clean FUTURE delivery occurrences that land on a weekday the
household's plan no longer delivers on ("wrong-day" occurrences).

These are the leftovers that make people show up on the wrong day's Purchase
Order after a cadence change. They come in two flavours:

  * **uncommitted** -- a SCHEDULED ``OrderSchedule`` on a non-plan weekday with
    NO live ``DeliveryOrder`` yet. Safe to drop: re-running
    ``sync_delivery_calendar`` for the enrollment removes it (and adds any
    missing correct-day dates), while leaving committed dates untouched.
  * **committed** -- the (member, date) is already batched into a live
    ``DeliveryOrder`` (i.e. a PO was cut for the old day before the cadence was
    fixed). The rebuild intentionally NEVER removes these, so the household can
    be double-delivered that week (old committed day + new correct day). These
    are only REPORTED here (cancelling a committed PO line is a separate,
    deliberate action) unless ``--cancel-committed`` is passed.

Broader than ``reassign_mismatched_cadence``: that command only fixes households
whose stored cadence didn't match their kitchen. This one catches ANY enrollment
whose calendar has drifted off its plan's weekdays, regardless of cause.

Usage:
    python manage.py clean_stale_delivery_dates                    # DRY RUN
    python manage.py clean_stale_delivery_dates --apply             # remove uncommitted
    python manage.py clean_stale_delivery_dates --apply --cancel-committed
"""
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    DeliveryOrder,
    DeliveryOrderStatus,
    EnrollmentStage,
    OrderSchedule,
    OrderStatus,
)
from api.services.orders import _WEEKDAY_CODES, sync_delivery_calendar

_TERMINAL_STAGES = (
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
)
_WD_LABEL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Command(BaseCommand):
    help = (
        "Detect and clean future delivery occurrences on a weekday the plan no "
        "longer delivers on. Removes uncommitted ones via sync_delivery_calendar; "
        "reports committed ones (double-delivery risk). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--cancel-committed", action="store_true",
            help="Also cancel committed old-day DeliveryOrders (prevents double delivery).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        cancel_committed = options["cancel_committed"]
        today = timezone.localdate()

        # 1. Scan every future SCHEDULED occurrence on a non-terminal enrollment
        #    and keep only those whose weekday isn't in their plan's weekdays.
        qs = (
            OrderSchedule.objects.filter(
                status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            )
            .exclude(enrollment__stage__in=_TERMINAL_STAGES)
            .select_related("enrollment", "kitchen", "member", "member__client")
        )
        wrong_by_enr = defaultdict(list)   # enrollment -> [occurrence, ...]
        pairs = set()                       # (client_id, date) for batched lookup
        for o in qs.iterator(chunk_size=2000):
            wanted = {
                _WEEKDAY_CODES[w]
                for w in (o.enrollment.delivery_weekdays or [])
                if w in _WEEKDAY_CODES
            }
            if not wanted or o.anticipated_delivery_date.weekday() in wanted:
                continue
            wrong_by_enr[o.enrollment].append(o)
            cid = o.member.client_id if o.member_id else None
            if cid:
                pairs.add((cid, o.anticipated_delivery_date))

        # 2. Which of those (client, date) pairs are already committed to a live
        #    DeliveryOrder (untouchable by the rebuild).
        batched = set()
        if pairs:
            client_ids = {c for c, _ in pairs}
            dates = {d for _, d in pairs}
            for cid, d in (
                DeliveryOrder.objects.filter(
                    member_id__in=client_ids, expected_delivery_date__in=dates
                )
                .exclude(status=DeliveryOrderStatus.CANCELLED)
                .values_list("member_id", "expected_delivery_date")
            ):
                batched.add((cid, d))

        report = Counter()
        committed_rows = []   # (client_id, kitchen, date, weekday)
        uncommitted_rows = []
        for enr, occ in wrong_by_enr.items():
            for o in occ:
                cid = o.member.client_id if o.member_id else None
                row = (
                    str(cid) if cid else "?",
                    o.kitchen.name if o.kitchen_id else "None",
                    o.anticipated_delivery_date.isoformat(),
                    _WD_LABEL[o.anticipated_delivery_date.weekday()],
                )
                if cid and (cid, o.anticipated_delivery_date) in batched:
                    report["committed"] += 1
                    committed_rows.append(row)
                else:
                    report["uncommitted"] += 1
                    uncommitted_rows.append(row)

        report["enrollments"] = len(wrong_by_enr)

        with transaction.atomic():
            removed_total = cancelled_total = 0
            # 3. Clean the uncommitted ones: a per-enrollment resync drops any
            #    SCHEDULED occurrence not on a plan weekday (and not batched),
            #    and adds any missing correct-day dates.
            for enr in wrong_by_enr:
                res = sync_delivery_calendar(enr)
                removed_total += res["removed"]

            # 4. Optionally cancel the committed old-day delivery orders so the
            #    household isn't delivered twice that week.
            if cancel_committed and pairs:
                committed_pairs = [(c, d) for (c, d) in pairs if (c, d) in batched]
                for cid, d in committed_pairs:
                    n = (
                        DeliveryOrder.objects.filter(
                            member_id=cid, expected_delivery_date=d
                        )
                        .exclude(status=DeliveryOrderStatus.CANCELLED)
                        .update(status=DeliveryOrderStatus.CANCELLED)
                    )
                    cancelled_total += n
                # Drop the now-freed SCHEDULED occurrences on those dates too.
                for enr, occ in wrong_by_enr.items():
                    for o in occ:
                        c = o.member.client_id if o.member_id else None
                        if c and (c, o.anticipated_delivery_date) in batched:
                            OrderSchedule.objects.filter(pk=o.pk).delete()

            self._report(report, uncommitted_rows, committed_rows,
                         removed_total, cancelled_total, apply, cancel_committed)

            if not apply:
                transaction.set_rollback(True)

    def _report(self, report, uncommitted, committed, removed, cancelled,
                apply, cancel_committed):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Clean stale delivery dates ==="))
        self.stdout.write(f"  {'Enrollments with wrong-day dates':<36}: {report.get('enrollments', 0)}")
        self.stdout.write(f"  {'Uncommitted wrong-day occurrences':<36}: {report.get('uncommitted', 0)}")
        self.stdout.write(f"  {'Committed (double-delivery risk)':<36}: {report.get('committed', 0)}")
        self.stdout.write(f"  {'Occurrences removed (this run)':<36}: {removed}")
        if cancel_committed:
            self.stdout.write(f"  {'Committed delivery orders cancelled':<36}: {cancelled}")

        if uncommitted:
            self.stdout.write(head(f"\nUncommitted -- removed by resync ({len(uncommitted)}, up to 30):"))
            for cid, k, d, wd in uncommitted[:30]:
                self.stdout.write(f"  {cid}  {k}  {d} ({wd})")
        if committed:
            verb = "cancelled" if cancel_committed else "KEPT (needs review)"
            self.stdout.write(head(f"\nCommitted -- {verb} ({len(committed)}, up to 30):"))
            for cid, k, d, wd in committed[:30]:
                self.stdout.write(f"  {cid}  {k}  {d} ({wd})")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))
