"""Report / purge cases that should never have been imported: cases created by a
Unite Us agent who is NOT on a CareCircle team (i.e. Met Council Team).

The CSV import only lets in cases whose creator is on a CareCircle team
(CARECIRCLE_ALLOWLIST_TEAMS); Met Council + unknown creators are blocked. But cases
imported BEFORE the UniteUsAgent roster was populated slipped in (the gate is
bypassed while the allowlist is empty). This removes those legacy rows.

TARGET (deliberately precise + safe): a case whose ``created_by_id`` resolves to a
KNOWN UniteUsAgent whose ``originating_team`` is NOT a CareCircle team. This means:
  * Extension-created cases are NOT touched -- the ext stamps created_by_id with a
    CareCircle *Agent* id (not a UniteUsAgent.user_id), so they never match.
  * Cases with no creator, or a creator not in the UniteUsAgent roster, are NOT
    touched (could be legit ext/admin/older rows) -- reported only.

Deleting a Case is SET_NULL for enrollments/notes/tickets/timeline (they survive,
just unlinked) and CASCADE only for its Unite Us contracted-services rows.

Review-only by default:
    python manage.py purge_non_carecircle_cases
Apply (delete):
    python manage.py purge_non_carecircle_cases --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, EnrollmentVerification, UniteUsAgent
from api.services.csv_import import CARECIRCLE_ALLOWLIST_TEAMS


class Command(BaseCommand):
    help = "Report/purge cases created by non-CareCircle (Met Council) Unite Us agents."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete (default: review only).")

    def handle(self, *args, **options):
        apply = options["apply"]

        carecircle_ids = set(
            UniteUsAgent.objects.filter(originating_team__in=CARECIRCLE_ALLOWLIST_TEAMS)
            .values_list("user_id", flat=True)
        )
        if not carecircle_ids:
            self.stderr.write(self.style.ERROR(
                "Abort: no CareCircle-team agents in the roster (allowlist empty) -- "
                "refusing to purge, or everything would be classified non-CareCircle."
            ))
            return

        # KNOWN Unite Us agents NOT on a CareCircle team -> the safe target set.
        non_cc = {
            str(uid): (team or "")
            for uid, team in UniteUsAgent.objects.exclude(
                originating_team__in=CARECIRCLE_ALLOWLIST_TEAMS
            ).values_list("user_id", "originating_team")
        }
        non_cc_ids = set(non_cc)

        target = Case.objects.filter(created_by_id__in=non_cc_ids)
        n = target.count()

        by_team = Counter()
        by_type = Counter()
        client_ids = set()
        for cid, ctype, client_id in target.values_list("created_by_id", "case_type", "client_id"):
            by_team[non_cc.get(str(cid), "?")] += 1
            by_type[ctype or "(blank)"] += 1
            client_ids.add(client_id)

        # Clients whose EVERY case is a target -> they'd be left fully caseless.
        caseless_after = 0
        for client_id in client_ids:
            total = Case.objects.filter(client_id=client_id).count()
            tgt = target.filter(client_id=client_id).count()
            if total == tgt:
                caseless_after += 1
        enr_unlinked = EnrollmentVerification.objects.filter(case__in=target).count()

        self.stdout.write(f"Cases created by non-CareCircle Unite Us agents: {n}")
        self.stdout.write(f"  by creator team : {dict(by_team)}")
        self.stdout.write(f"  by case type    : {dict(by_type)}")
        self.stdout.write(f"  distinct clients affected     : {len(client_ids)}")
        self.stdout.write(f"  clients left fully caseless    : {caseless_after}")
        self.stdout.write(f"  enrollments that get case=NULL : {enr_unlinked}")

        if not apply:
            self.stdout.write("")
            self.stdout.write("Review only. Re-run with --apply to DELETE these cases.")
            return

        with transaction.atomic():
            deleted, _ = target.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s) (cases + cascaded)."))
