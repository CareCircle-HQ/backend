"""Reconcile blank-provider internal-service cases against Met Council's export.

Background
----------
Some INTERNAL-SERVICE (meal/box) cases were imported (before the strict org
gate existed) with EVERY provider column blank -- no managing provider, no
originating provider. ``case_is_met_council`` leniently treats a blank-manager
meal case as Met Council's (meal/box programs are Met Council's own), which is
correct for the many legit cases imported without provider columns -- but it
also keeps genuinely non-Met cases that merely happen to have blank providers.

The AUTHORITATIVE separator is Met Council's own case export: a case that Met
Council originates or manages appears in that export. The export contains only
NON-closed cases (managed / referred / pending / declined / ...), so:

* A blank-manager internal case that is ACTIVE (not closed/cancelled) but is
  ABSENT from the export is NOT Met Council's -> remove it.
* One that IS in the export is Met Council's -> keep.
* A CLOSED one can't be judged by the export (closed cases aren't exported)
  -> keep (harmless history).

Safety
------
Never remove a case for a member who is actively being served: if the client
has a SERVICE_ACTIVE enrollment or a SCHEDULED delivery, the case is SKIPPED
and reported for manual review instead of deleted.

Dry-run by default; pass ``--apply`` to delete.

Usage
-----
    python manage.py reconcile_internal_cases_against_export \
        --export /path/to/cases_export.csv
    python manage.py reconcile_internal_cases_against_export \
        --export /path/to/cases_export.csv --apply
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from api.models import (
    Case,
    CaseStatus,
    CaseType,
    EnrollmentStage,
    EnrollmentVerification,
    MemberDeliverySchedule,
    ScheduleStatus,
)

TERMINAL_STATUSES = {CaseStatus.CLOSED, CaseStatus.CANCELLED}


class Command(BaseCommand):
    help = (
        "Remove blank-provider internal-service cases that are active but absent "
        "from Met Council's authoritative case export (and not actively served)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--export", required=True,
            help="Path to Met Council's cases export CSV (source of truth).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without this the command only previews.",
        )
        parser.add_argument(
            "--limit", type=int, default=25,
            help="How many sample rows to print in the preview (default 25).",
        )

    def _load_export_ids(self, path):
        """Return the set of case_ids present in the export CSV."""
        try:
            fh = open(path, newline="")
        except OSError as exc:
            raise CommandError(f"Cannot open export: {exc}")
        ids = set()
        with fh:
            reader = csv.DictReader(fh)
            if "case_id" not in (reader.fieldnames or []):
                raise CommandError(
                    "Export is missing a 'case_id' column -- is this a cases export?"
                )
            for row in reader:
                cid = (row.get("case_id") or "").strip()
                if cid:
                    ids.add(cid)
        return ids

    def _served_client_ids(self, client_ids):
        """Clients (subset of client_ids) who are actively being served -- a
        SERVICE_ACTIVE enrollment OR a SCHEDULED delivery. Their cases are never
        auto-deleted."""
        served = set(
            EnrollmentVerification.objects
            .filter(client_id__in=client_ids, stage=EnrollmentStage.SERVICE_ACTIVE)
            .values_list("client_id", flat=True)
        )
        served |= set(
            MemberDeliverySchedule.objects
            .filter(enrollment__client_id__in=client_ids,
                    status=ScheduleStatus.SCHEDULED)
            .values_list("enrollment__client_id", flat=True)
        )
        return served

    def handle(self, *args, **opts):
        head = self.style.MIGRATE_HEADING
        export_ids = self._load_export_ids(opts["export"])
        self.stdout.write(head(
            f"Loaded {len(export_ids):,} case_id(s) from the export."
        ))

        # Blank-manager internal-service cases -- exactly the set that
        # case_is_met_council leniently keeps (provider FK null AND name blank).
        blank_manager = Q(provider_id__isnull=True) & Q(provider_name="")
        candidates = (
            Case.objects
            .filter(case_type=CaseType.INTERNAL_SERVICE)
            .filter(blank_manager)
        )
        total = candidates.count()
        self.stdout.write(
            f"Blank-provider internal-service cases in DB: {total:,}"
        )

        # Partition.
        in_export = kept_closed = doomed = 0
        doomed_ids = []
        active_absent_clients = set()
        for c in candidates.only(
            "case_id", "client_id", "case_status", "program_name"
        ).iterator():
            cid = str(c.case_id)
            if cid in export_ids:
                in_export += 1
                continue
            if c.case_status in TERMINAL_STATUSES:
                kept_closed += 1
                continue
            # Active + absent from export -> not Met Council's (candidate).
            doomed += 1
            doomed_ids.append(c.case_id)
            active_absent_clients.add(c.client_id)

        # Protect actively-served members.
        served = self._served_client_ids(active_absent_clients)
        deletable, protected = [], []
        for c in Case.objects.filter(case_id__in=doomed_ids).only(
            "case_id", "client_id", "case_status", "program_name"
        ):
            (protected if c.client_id in served else deletable).append(c)

        self.stdout.write(head("\n=== Reconciliation summary ==="))
        self.stdout.write(f"  in export (Met Council, kept)      : {in_export:,}")
        self.stdout.write(f"  closed/cancelled (kept as history) : {kept_closed:,}")
        self.stdout.write(f"  ACTIVE + absent from export        : {doomed:,}")
        self.stdout.write(
            f"    -> deletable (not served)        : {len(deletable):,}"
        )
        self.stdout.write(self.style.WARNING(
            f"    -> PROTECTED (actively served)   : {len(protected):,}"
        ))

        limit = opts["limit"]
        if deletable:
            self.stdout.write(head("\n  Sample deletable cases:"))
            for c in deletable[:limit]:
                self.stdout.write(
                    f"    {c.case_id} | {c.case_status} | client {c.client_id} "
                    f"| {c.program_name[:70]}"
                )
        if protected:
            self.stdout.write(self.style.WARNING(
                "\n  Sample PROTECTED cases (served -- review manually):"
            ))
            for c in protected[:limit]:
                self.stdout.write(
                    f"    {c.case_id} | {c.case_status} | client {c.client_id} "
                    f"| {c.program_name[:70]}"
                )

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- no changes made. Re-run with --apply to delete the "
                "deletable set (protected/served cases are never auto-deleted)."
            ))
            return

        if not deletable:
            self.stdout.write(self.style.SUCCESS("\nNothing to delete."))
            return

        ids = [c.case_id for c in deletable]
        with transaction.atomic():
            deleted, _ = Case.objects.filter(case_id__in=ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {len(ids):,} case(s) ({deleted:,} row(s) incl. children). "
            f"Protected {len(protected):,} served case(s)."
        ))
