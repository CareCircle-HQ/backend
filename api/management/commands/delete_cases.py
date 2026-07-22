"""Delete cases by explicit id and/or by owning client id.

A targeted, reusable cleanup tool: you pass the case id(s) and/or client id(s)
on the command line and it deletes the matching cases. Dry-run by default
(prints exactly what WOULD be deleted); pass --apply to commit.

Examples
--------
# Preview every case belonging to two clients:
python manage.py delete_cases --client <client_uuid> --client <client_uuid>

# Delete specific cases:
python manage.py delete_cases --case <case_uuid> --case <case_uuid> --apply

# Delete cases from a file (one id per line), mixing clients + cases:
python manage.py delete_cases --client-file clients.txt --case-file cases.txt --apply

SAFETY: a case backing a verification enrollment (EnrollmentVerification.case)
is NEVER deleted by default -- deleting it would orphan the delivery (FK is
on_delete=SET_NULL). Pass --force-enrollment-linked to override. Everything runs
in one transaction and affected clients' funnel stages are recomputed afterward.
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from api.models import Case, Client


def _read_ids(path):
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


class Command(BaseCommand):
    help = (
        "Delete cases by --case id and/or --client id. Dry-run by default; "
        "pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--case", action="append", default=[], metavar="CASE_ID",
            help="A case id to delete (repeatable).",
        )
        parser.add_argument(
            "--client", action="append", default=[], metavar="CLIENT_ID",
            help="Delete ALL cases owned by this client id (repeatable).",
        )
        parser.add_argument(
            "--case-file", help="File with one case id per line.",
        )
        parser.add_argument(
            "--client-file", help="File with one client id per line.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete. Without this the command only previews.",
        )
        parser.add_argument(
            "--force-enrollment-linked", action="store_true",
            help=(
                "Override the safety guard and ALSO delete cases that back a "
                "verification enrollment (default: such cases are preserved)."
            ),
        )

    def handle(self, *args, **opts):
        case_ids = list(opts["case"])
        client_ids = list(opts["client"])
        if opts["case_file"]:
            case_ids += _read_ids(opts["case_file"])
        if opts["client_file"]:
            client_ids += _read_ids(opts["client_file"])
        if not case_ids and not client_ids:
            raise CommandError(
                "Provide at least one --case/--client (or --case-file/--client-file)."
            )

        # Validate the client ids exist (a typo'd client should be a hard stop,
        # not a silent no-op that hides a mistake on a prod deletion).
        missing = [
            cid for cid in client_ids
            if not Client.objects.filter(pk=cid).exists()
        ]
        if missing:
            raise CommandError(
                "These client id(s) do not exist:\n  " + "\n  ".join(missing)
            )

        selector = Q()
        if case_ids:
            selector |= Q(pk__in=case_ids)
        if client_ids:
            selector |= Q(client_id__in=client_ids)
        qs = Case.objects.filter(selector)

        protect = not opts["force_enrollment_linked"]
        if protect:
            protected = qs.filter(enrollments__isnull=False).distinct()
            pcount = protected.count()
            if pcount:
                self.stdout.write(self.style.WARNING(
                    f"Safety guard: {pcount} enrollment-backed case(s) will be "
                    f"PRESERVED (use --force-enrollment-linked to override):"
                ))
                for c in protected:
                    self.stdout.write(
                        f"    case {c.case_id} (client {c.client_id}, "
                        f"status {c.case_status})"
                    )
            qs = qs.exclude(enrollments__isnull=False)

        n = qs.count()
        # Report any explicitly-named case ids that matched nothing.
        if case_ids:
            found = set(str(x) for x in qs.filter(pk__in=case_ids)
                        .values_list("pk", flat=True))
            for cid in case_ids:
                if cid not in found:
                    self.stdout.write(f"  note: case {cid} not deletable (absent or protected)")

        if n == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to delete."))
            return

        by_type = Counter(qs.values_list("case_type", flat=True))
        self.stdout.write(
            f"{n} case(s) would be deleted "
            f"({len(client_ids)} client(s) + {len(case_ids)} explicit case id(s) requested)."
        )
        self.stdout.write("  By case_type:")
        for ct, c in by_type.most_common():
            self.stdout.write(f"    {c:6}  {ct or '(blank)'}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to delete."
            ))
            return

        affected = list(qs.values_list("client_id", flat=True).distinct())
        with transaction.atomic():
            deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {n} case(s) ({deleted} row(s) incl. children)."
        ))

        from api.services.lifecycle import recompute_client_stage

        recomputed = 0
        for client in Client.objects.filter(pk__in=affected):
            try:
                recompute_client_stage(client)
                recomputed += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  stage recompute failed for {client.pk}: {exc}")
        self.stdout.write(self.style.SUCCESS(
            f"Recomputed lifecycle stage for {recomputed} affected client(s)."
        ))
