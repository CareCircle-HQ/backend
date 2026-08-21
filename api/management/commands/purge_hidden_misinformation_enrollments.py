"""Delete the caseless placeholder "previous enrollments" flagged as
misinformation by ``resolve_caseless_previous_enrollments``.

Only rows that are ALL of: ``hidden_misinformation=True``, ``case IS NULL`` and
in a TERMINAL stage (closed / cancelled / disregarded) are eligible -- so a live
or case-backed enrollment can never be removed. Before deleting, any survivor
whose ``supersedes`` points AT a purged row is re-pointed to that row's own
predecessor (skipping other purged rows), so the supersession chain stays
continuous. Cascades remove the placeholder's own member profiles / schedules /
orders / stage events (all CASCADE); timeline events + warnings are SET_NULL.

DRY-RUN by default (reports what WOULD be deleted); pass ``--apply`` to commit.
Use ``--client <id>`` to scope to one member. Idempotent.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from api.models import (
    EnrollmentStage, EnrollmentVerification, MemberDeliverySchedule,
    MemberDietaryProfile,
)

_TERMINAL_STAGES = [
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
]


class Command(BaseCommand):
    help = (
        "Delete caseless placeholder enrollments flagged hidden_misinformation "
        "(terminal + no case), re-pointing supersession chains (dry-run; --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Commit the deletes (default is a dry-run that changes nothing).",
        )
        parser.add_argument(
            "--client", default="",
            help="Only purge this client_id's flagged rows (default: all).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        eligible = EnrollmentVerification.objects.filter(
            hidden_misinformation=True,
            case__isnull=True,
            stage__in=[s.value for s in _TERMINAL_STAGES],
        )
        if only:
            eligible = eligible.filter(client__client_id=only)

        flagged_ids = set(eligible.values_list("pk", flat=True))
        if not flagged_ids:
            self.stdout.write("Nothing to purge (no eligible flagged rows).")
            return

        # Safety report: how many flagged rows are NOT terminal / still have a case
        # (excluded above) -- surfaced so an operator knows they were skipped.
        skipped = EnrollmentVerification.objects.filter(hidden_misinformation=True)
        if only:
            skipped = skipped.filter(client__client_id=only)
        skipped_count = skipped.exclude(pk__in=flagged_ids).count()

        # Re-point survivors whose supersedes points into the purged set to the
        # nearest surviving predecessor (skip other purged rows), so no chain is
        # left dangling by the SET_NULL on delete.
        survivors = (
            EnrollmentVerification.objects
            .filter(supersedes_id__in=flagged_ids)
            .exclude(pk__in=flagged_ids)
            .select_related("supersedes")
        )
        repointed = 0
        for s in survivors:
            cur = s.supersedes
            seen = set()
            while cur is not None and cur.pk in flagged_ids and cur.pk not in seen:
                seen.add(cur.pk)
                cur = cur.supersedes
            new_id = cur.pk if cur is not None else None
            if s.supersedes_id != new_id:
                repointed += 1
                if apply:
                    s.supersedes_id = new_id
                    s.save(update_fields=["supersedes"])

        # Cascade footprint (for transparency in the report). Counted separately
        # -- aggregating multiple reverse relations in one query multiplies the
        # JOINs and inflates the numbers.
        n_profiles = MemberDietaryProfile.objects.filter(enrollment_id__in=flagged_ids).count()
        n_deliveries = MemberDeliverySchedule.objects.filter(enrollment_id__in=flagged_ids).count()

        self.stdout.write(
            f"Eligible flagged rows to delete: {len(flagged_ids)}\n"
            f"  chain links re-pointed        : {repointed}\n"
            f"  cascades -> member_profiles {n_profiles}, delivery_schedules "
            f"{n_deliveries} (+ schedules/orders/stage_events)\n"
            f"  flagged-but-not-eligible (skipped): {skipped_count}"
        )

        if not apply:
            self.stdout.write(self.style.SUCCESS(
                "\nDRY-RUN (no changes written). Re-run with --apply to delete."
            ))
            return

        with transaction.atomic():
            deleted, per_model = eligible.delete()
        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: deleted {deleted} row(s) across {len(per_model)} table(s); "
            f"re-pointed {repointed} chain link(s)."
        ))

        # A large DELETE leaves stale planner statistics (and dead tuples). Without
        # a follow-up ANALYZE, Postgres can flip a hot query -- notably the Members
        # list, which sorts every client -- onto a catastrophic plan; that took the
        # site down after the first run of this purge. Refresh stats on the tables
        # this touches AND the ones the Members/verification queries join, so the
        # planner stays honest. (ANALYZE is online and transaction-safe.)
        self.stdout.write("Refreshing planner statistics (ANALYZE)...")
        analyze_tables = [
            "api_enrollmentverification", "api_memberdietaryprofile",
            "api_memberdeliveryschedule", "api_orderschedule",
            "api_case", "api_client", "api_householdmember", "api_stageevent",
        ]
        with connection.cursor() as cur:
            for tbl in analyze_tables:
                try:
                    cur.execute(f"ANALYZE {tbl};")
                except Exception as exc:  # pragma: no cover - defensive
                    self.stderr.write(f"  ANALYZE {tbl} failed: {exc}")
        self.stdout.write(self.style.SUCCESS(
            "Statistics refreshed. (For heavy bloat also run VACUUM (ANALYZE) "
            "on these tables; VACUUM cannot run here inside a transaction.)"
        ))
