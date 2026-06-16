"""Backfill TimelineEvent rows from existing domain data.

Generates the central-history events (consent, insurance, social care coverage,
screening, assessment, case) for records that already exist, so the manager
dashboard timeline is populated for historical data. Idempotent: emission upserts
on a dedupe_key, so re-running won't duplicate events.

    python manage.py backfill_timeline
    python manage.py backfill_timeline --client-id <uuid>   # one client (repeatable)
"""

from django.core.management.base import BaseCommand

from api.history import ChangeSource
from api.models import (
    Assessment,
    Case,
    Client,
    Insurance,
    Screening,
    SocialCareCoverage,
)
from api.services import timeline

SRC = ChangeSource.SYSTEM
ACTOR = "system:backfill"


class Command(BaseCommand):
    help = "Generate TimelineEvent rows from existing clients/insurance/screenings/etc."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client-id", type=str, action="append", default=None,
            help="Backfill only the given client id(s); repeatable.",
        )

    def handle(self, *args, **options):
        client_qs = Client.objects.all()
        if options["client_id"]:
            client_qs = client_qs.filter(client_id__in=options["client_id"])

        counts = {k: 0 for k in
                  ("consent", "insurance", "coverage", "screening", "assessment", "case")}

        for client in client_qs.iterator():
            if timeline.event_for_consent(client, source=SRC, actor=ACTOR):
                counts["consent"] += 1
            for ins in Insurance.objects.filter(client=client):
                if timeline.event_for_insurance(ins, source=SRC, actor=ACTOR):
                    counts["insurance"] += 1
            for scc in SocialCareCoverage.objects.filter(client=client):
                if timeline.event_for_social_care_coverage(scc, source=SRC, actor=ACTOR):
                    counts["coverage"] += 1

        for s in Screening.objects.exclude(client__isnull=True).iterator():
            if options["client_id"] and str(s.client_id) not in options["client_id"]:
                continue
            if timeline.event_for_screening(s, source=SRC, actor=ACTOR):
                counts["screening"] += 1

        for a in Assessment.objects.exclude(client__isnull=True).iterator():
            if options["client_id"] and str(a.client_id) not in options["client_id"]:
                continue
            if timeline.event_for_assessment(a, source=SRC, actor=ACTOR):
                counts["assessment"] += 1

        case_qs = Case.objects.all()
        if options["client_id"]:
            case_qs = case_qs.filter(client_id__in=options["client_id"])
        for c in case_qs.iterator():
            if timeline.event_for_case(c, source=SRC, actor=ACTOR):
                counts["case"] += 1

        total = sum(counts.values())
        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {total} timeline events: {counts}"
        ))
