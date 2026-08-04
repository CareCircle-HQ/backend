"""Case-table hygiene: delete cases the current import would NOT accept.

The import keeps a case only when BOTH hold (see api.services.csv_import +
api.serializers.case_in_import_scope + api.services.lifecycle.case_is_met_council):

  * Met Council owns it (managed by Met Council, or an internal-service case with
    no named managing org), AND
  * it is IN PROGRAM SCOPE -- our meal/box service, or a program in the
    ActiveProgram table whose category we track (Internal Service / Eligibility /
    Reauthorization / Care Management / Screening). External Services, "Other",
    blank, or programs not in the table are out of scope.

Legacy rows imported before these gates existed remain in the DB. This removes
them, leaving only the cases the importer would accept today. Both rules are
re-derived LIVE from each stored case (authoritative), so a case that is
actually in scope is never dropped.

SAFETY: a case that backs a verification enrollment (EnrollmentVerification.case)
is NEVER deleted by default (deleting would orphan a live delivery -- the FK is
SET_NULL). Pass --force-enrollment-linked to override.

Dry-run by default; pass --apply to commit. Deleting a Case cascades to its
ContractedService rows; every other relation is SET_NULL. Affected clients' funnel
stages are recomputed afterwards.

Usage:
    python manage.py purge_out_of_scope_cases
    python manage.py purge_out_of_scope_cases --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef

from api.models import (
    ActiveProgram, Case, CaseType, ContractedService, EnrollmentVerification,
)
from api.serializers import INTERNAL_SERVICE_SUBTYPES, _IN_SCOPE_CASE_CATEGORIES
from api.services.lifecycle import is_met_council_case


class Command(BaseCommand):
    help = (
        "Delete cases the current import would reject (not Met Council OR out of "
        "program scope). Dry-run by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit deletions.")
        parser.add_argument(
            "--force-enrollment-linked", action="store_true",
            help="Also delete cases that back a verification enrollment (default: preserved).",
        )

    # --- keep predicate (mirrors the import gate) ---------------------------
    def _load_scope_map(self):
        return {
            (p.program_name or "").strip().casefold(): (p.case_category or "").strip().casefold()
            for p in ActiveProgram.objects.all()
        }

    def _in_scope(self, service_type, program_name, prog_cat):
        if (service_type or "").strip().casefold() in INTERNAL_SERVICE_SUBTYPES:
            return True
        return prog_cat.get((program_name or "").strip().casefold()) in _IN_SCOPE_CASE_CATEGORIES

    def _is_met(self, case_type, provider_id, provider_name):
        if is_met_council_case(provider_id=provider_id, provider_name=provider_name,
                               allow_originating=False):
            return True
        # internal-service case with no named managing org == Met Council's own.
        if case_type == CaseType.INTERNAL_SERVICE and not provider_id and not (provider_name or "").strip():
            return True
        return False

    def handle(self, *args, **options):
        apply = options["apply"]
        protect = not options["force_enrollment_linked"]
        prog_cat = self._load_scope_map()

        enr_backed = EnrollmentVerification.objects.filter(case_id=OuterRef("pk"))
        qs = (
            Case.objects.annotate(_enr=Exists(enr_backed))
            .values_list("case_id", "case_type", "provider_id", "provider_name",
                         "service_type", "program_name", "_enr")
        )

        total = 0
        doomed = []          # case_ids to delete
        protected = 0        # would-be-doomed but enrollment-backed (kept)
        by_type = Counter()
        by_org = Counter()
        by_reason = Counter()
        for cid, ct, pid, pname, stype, prog, enr in qs.iterator(chunk_size=5000):
            total += 1
            met = self._is_met(ct, pid, pname)
            scope = self._in_scope(stype, prog, prog_cat)
            if met and scope:
                continue  # importer would keep it
            if enr and protect:
                protected += 1
                continue
            doomed.append(cid)
            by_type[ct or "(blank)"] += 1
            by_org[(pname or "(blank)")] += 1
            by_reason[
                "not Met Council" if not met else "out of program scope"
            ] += 1

        clients = (
            Case.objects.filter(case_id__in=doomed)
            .values("client_id").distinct().count()
        )
        cs_count = ContractedService.objects.filter(case_id__in=doomed).count()

        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Purge out-of-scope cases ==="))
        self.stdout.write(f"  total cases:                 {total}")
        self.stdout.write(f"  WOULD DELETE:                {len(doomed)}")
        self.stdout.write(f"  preserved (enrollment-backed): {protected}"
                          + ("" if protect else " [--force: none preserved]"))
        self.stdout.write(f"  distinct clients affected:   {clients}")
        self.stdout.write(f"  contracted services (cascade): {cs_count}")
        self.stdout.write("  by reason:")
        for k, v in by_reason.most_common():
            self.stdout.write(f"     {v:7}  {k}")
        self.stdout.write("  by case_type:")
        for k, v in by_type.most_common():
            self.stdout.write(f"     {v:7}  {k}")
        self.stdout.write("  by managing org (top 12):")
        for k, v in by_org.most_common(12):
            self.stdout.write(f"     {v:7}  {k}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing deleted. Re-run with --apply to commit."
            ))
            return
        if not doomed:
            self.stdout.write(self.style.SUCCESS("Nothing to delete."))
            return

        client_ids = list(
            Case.objects.filter(case_id__in=doomed)
            .values_list("client_id", flat=True).distinct()
        )

        def _delete_chunk(chunk):
            with transaction.atomic():
                ContractedService.objects.filter(case_id__in=chunk).delete()
                d, _ = Case.objects.filter(case_id__in=chunk).delete()
                return d

        deleted = 0
        for i in range(0, len(doomed), 500):
            chunk = doomed[i:i + 500]
            try:
                deleted += _delete_chunk(chunk)
            except IntegrityError:
                deleted += _delete_chunk(chunk)  # retry once (raced child insert)

        # Recompute funnel stage for affected clients so the member view is consistent.
        from api.models import Client
        from api.services.lifecycle import recompute_client_stage

        recomputed = 0
        for c in Client.objects.filter(pk__in=client_ids).iterator(chunk_size=500):
            try:
                recompute_client_stage(c)
                recomputed += 1
            except Exception:  # noqa: BLE001 - never let one bad client stop the sweep
                pass

        self.stdout.write(self.style.SUCCESS(
            f"\nAPPLIED: deleted {deleted} case(s); recomputed {recomputed} client stage(s)."
        ))
