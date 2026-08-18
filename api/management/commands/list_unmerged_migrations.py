"""Read-only diagnostic: find clients that were migrated in Unite Us BEFORE our
migration detection existed, so they were never merged.

A Unite Us person-migration re-parents the person's CASES to the NEW canonical
id while our internal service state (the ENROLLMENT) stays on the OLD id. That
leaves a torn link -- ``enrollment.client != enrollment.case.client`` -- which
``merge_migrated_client`` was built to heal. Anything migrated before detection
still shows that tear.

  OLD = enrollment.client (our service state)  ->  NEW = enrollment.case.client

DOB is the tiebreaker: a real migration is the SAME person, so old/new share a
date of birth. A torn link with a DIFFERENT (or missing) DOB is NOT a migration
-- it's an enrollment wrongly linked to a household relative's case -- so it is
listed separately as REVIEW and must NOT be re-saved/merged.

Makes NO changes. Prints the OLD client id (open THAT profile in Unite Us and
re-save so the extension captures the 301 -> new migration) plus the NEW id.
"""

from django.core.management.base import BaseCommand
from django.db.models import F

from api.models import EnrollmentVerification


class Command(BaseCommand):
    help = (
        "Print clients with an unmerged Unite Us migration (torn "
        "enrollment.client != case.client). Read-only; no changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids-only", action="store_true",
            help="Print ONLY the OLD client ids of genuine migrations (one per line).",
        )

    def handle(self, *args, **opts):
        rows = (
            EnrollmentVerification.objects
            .exclude(case__isnull=True)
            .exclude(case__client_id=F("client_id"))
            .select_related("client", "case__client")
            .order_by("client__last_name", "client__first_name")
        )

        migrations, review = [], []
        seen = set()
        for e in rows.iterator(chunk_size=1000):
            old, new = e.client, getattr(e.case, "client", None)
            if old is None or new is None:
                continue
            key = (str(old.client_id), str(new.client_id))
            if key in seen:
                continue
            seen.add(key)
            same_dob = (
                old.date_of_birth is not None
                and old.date_of_birth == new.date_of_birth
            )
            (migrations if same_dob else review).append((old, new))

        if opts["ids_only"]:
            for old, _new in migrations:
                self.stdout.write(str(old.client_id))
            return

        def name(c):
            return f"{(c.first_name or '').strip()} {(c.last_name or '').strip()}".strip()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"MIGRATIONS -- same DOB, safe to re-save/merge ({len(migrations)}):"
        ))
        self.stdout.write(f"  {'OLD client_id (open this)':<40}{'NEW client_id':<40}name / dob")
        for old, new in migrations:
            self.stdout.write(
                f"  {str(old.client_id):<40}{str(new.client_id):<40}"
                f"{name(old)} -> {name(new)}  (dob {old.date_of_birth})"
            )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            f"REVIEW -- DOB differs/missing, NOT a migration, do NOT merge ({len(review)}):"
        ))
        for old, new in review:
            self.stdout.write(
                f"  {str(old.client_id):<40}{str(new.client_id):<40}"
                f"{name(old)} (dob {old.date_of_birth}) -> {name(new)} (dob {new.date_of_birth})"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(migrations)} migration(s) to re-save; {len(review)} to review manually."
        ))
