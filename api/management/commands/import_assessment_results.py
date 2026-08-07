"""Headless assessment-results enrichment (nightly-assessment-eligibility plan,
Phases 3 + 5 -- serves both nightly backfill and ad-hoc runs).

Pulls each tracked client's eligibility assessments from the Unite Us
screenings-ingestion host (the SAME source the browser extension reads) and
upserts their ``eligible_services`` / ``eligible_status`` through
``AssessmentSerializer`` -- which already drives ``catalog.upsert_programs`` and
``derive_client_level`` (Client Level 1/2). Shares its core with the nightly
Celery task via ``api.services.assessment_enrichment``.

    # preview what WOULD be enriched for one client
    python manage.py import_assessment_results --client <person_id>

    # backfill (write) every assessment still missing results
    python manage.py import_assessment_results --apply

    # only recent imports, capped
    python manage.py import_assessment_results --apply --since 2026-08-01 --limit 200

Previews by default (dry-run); pass ``--apply`` to write. Read-only auth by
default (never rotates the shared refresh token); pass ``--refresh`` to allow a
server-side token refresh off-hours.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from api.history import ChangeSource, change_context
from api.services.assessment_enrichment import enrich_assessments


class Command(BaseCommand):
    help = "Enrich assessments with eligible_services from the Unite Us screenings-ingestion API."

    def add_arguments(self, parser):
        parser.add_argument("--client", help="Enrich a single Unite Us person_id.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Write changes. Without this flag the command only previews (dry run).",
        )
        parser.add_argument(
            "--refresh", action="store_true",
            help="Allow a server-side token refresh (may log the live agent out).",
        )
        parser.add_argument("--provider", help="Scope to a provider_id (defaults to the credential's).")
        parser.add_argument("--since", help="Only assessments created on/after this date (YYYY-MM-DD).")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N clients (0 = all).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        since = None
        if opts.get("since"):
            since = parse_date(opts["since"])
            if since is None:
                raise CommandError(f"--since must be YYYY-MM-DD, got {opts['since']!r}")

        client_ids = [opts["client"]] if opts.get("client") else None
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Assessment results enrichment ({'APPLY' if apply else 'dry-run'}) ==="
        ))

        # Writes run inside the import change context so history rows are tagged
        # source='import'; the dry-run preview touches nothing.
        ctx = (
            change_context(ChangeSource.IMPORT, "system:assessment-results")
            if apply else _null_context()
        )
        with ctx:
            enricher = enrich_assessments(
                client_ids=client_ids,
                provider_id=opts.get("provider"),
                apply=apply,
                allow_refresh=opts["refresh"],
                limit=opts["limit"],
                since=since,
            )

        if enricher.cred is not None:
            self.stdout.write(
                f"  cred={enricher.cred.pk} provider_id={enricher.provider_id!r}"
            )
        if not apply:
            for p in enricher.previews:
                self.stdout.write(
                    f"  [dry-run] {p['person_id']} assessment {p['assessment_id']}: "
                    f"eligible_services={p['eligible_services']}"
                )

        s = enricher.stats
        self.stdout.write(self.style.SUCCESS(
            "\n{verb}: {clients} client(s), {assessments} assessment(s) seen, "
            "{enriched} with eligible_services, {errors} error(s).".format(
                verb="APPLIED" if apply else "WOULD ENRICH", **s
            )
        ))
        for err in enricher.errors:
            self.stderr.write(f"  {err}")
        if not apply:
            self.stdout.write("  (dry-run -- nothing written; pass --apply to write)")


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
