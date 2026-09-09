"""Seed ``Client.client_added_at`` from the member's earliest real activity.

``client_added_at`` is meant to answer "when did this member reach us?", and it is
stamped on first insert going forward. Existing rows can only be inferred, and the
useful evidence is the member's own first activity -- NOT when a bulk import
happened to write the row (which lumps tens of thousands onto one day) and NOT
``created_at`` (Unite Us's own date, which clusters on the few days they migrated
this population in).

Evidence, in the order the CRM lifecycle produces it:

1. earliest ``Screening.screen_created_at``
2. earliest ELIGIBILITY ``Case.case_created_at``
3. earliest INTERNAL SERVICE ``Case.case_created_at``
4. earliest ``HouseholdMember.added_at`` -- when we added them to a household
5. ``Client.created_at`` -- Unite Us's own date, LAST resort

The first source that has a date wins, and the level used is reported per client
so weak evidence is visible rather than hidden.

Levels 1-3 are real CRM activity. Levels 4-5 exist because ~11k members have no
screening, no case and no assessment -- imported contact records that never did
anything. For them ``created_at`` is all we know; it is often the date Unite Us
migrated this population in, which is imprecise but true (that IS when they
reached our world) and beats being invisible to every date filter. Pass
``--no-created-at-fallback`` to leave those blank instead.

Usage:

    python manage.py backfill_client_added_at --dry-run     # inspect, change nothing
    python manage.py backfill_client_added_at
    python manage.py backfill_client_added_at --only-earlier # never move a date forward
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Min

from api.models import Case, CaseType, Client, HouseholdMember, Screening

# Evidence order: real CRM activity first, then progressively weaker proxies.
CHAIN = (
    "screening",
    "eligibility_case",
    "internal_service_case",
    "household_added",
    "unite_us_created_at",
)
# Only these represent the member DOING something in the CRM; used to report when
# a later activity source held an earlier date.
ACTIVITY_LEVELS = ("screening", "eligibility_case", "internal_service_case")

BATCH = 2000


class Command(BaseCommand):
    help = "Seed Client.client_added_at from the earliest screening / eligibility / internal-service date."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--only-earlier", action="store_true",
            help="Only write when the derived date is EARLIER than the stored one.",
        )
        parser.add_argument(
            "--no-created-at-fallback", action="store_true",
            help="Skip level 5 (Unite Us created_at) and leave those members blank.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N clients (for a quick look).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        only_earlier = opts["only_earlier"]
        use_created_at = not opts["no_created_at_fallback"]
        limit = opts["limit"] or 0

        self.stdout.write("Collecting earliest dates per client…")

        # Three aggregate queries rather than per-client lookups across ~69k rows.
        screening = dict(
            Screening.objects.filter(screen_created_at__isnull=False)
            .values_list("client_id")
            .annotate(first=Min("screen_created_at"))
            .values_list("client_id", "first")
        )
        eligibility = dict(
            Case.objects.filter(
                case_type=CaseType.ELIGIBILITY, case_created_at__isnull=False
            )
            .values_list("client_id")
            .annotate(first=Min("case_created_at"))
            .values_list("client_id", "first")
        )
        internal = dict(
            Case.objects.filter(
                case_type=CaseType.INTERNAL_SERVICE, case_created_at__isnull=False
            )
            .values_list("client_id")
            .annotate(first=Min("case_created_at"))
            .values_list("client_id", "first")
        )
        household = dict(
            HouseholdMember.objects.filter(added_at__isnull=False)
            .values_list("client_id")
            .annotate(first=Min("added_at"))
            .values_list("client_id", "first")
        )
        self.stdout.write(
            f"  screenings: {len(screening)}  eligibility cases: {len(eligibility)}  "
            f"internal-service cases: {len(internal)}  household rows: {len(household)}"
        )

        used = Counter()
        years = Counter()
        skipped_no_evidence = 0
        skipped_not_earlier = 0
        would_move_forward = 0
        # How often a LATER source in the chain actually holds an earlier date --
        # reported so the priority order can be revisited on real numbers.
        better_available = 0

        batch, written = [], 0
        qs = Client.objects.all().only("client_id", "client_added_at", "created_at")
        if limit:
            qs = qs[:limit]

        for client in qs.iterator(chunk_size=BATCH):
            cid = client.client_id
            candidates = {
                "screening": screening.get(cid),
                "eligibility_case": eligibility.get(cid),
                "internal_service_case": internal.get(cid),
                "household_added": household.get(cid),
                # Weakest evidence, and only if allowed: Unite Us's own date.
                "unite_us_created_at": client.created_at if use_created_at else None,
            }
            chosen_source, chosen = None, None
            for name in CHAIN:
                if candidates[name] is not None:
                    chosen_source, chosen = name, candidates[name]
                    break

            if chosen is None:
                skipped_no_evidence += 1
                continue

            activity = [candidates[n] for n in ACTIVITY_LEVELS if candidates[n] is not None]
            if activity and min(activity) < chosen:
                better_available += 1

            current = client.client_added_at
            if current is not None and chosen > current:
                would_move_forward += 1
                if only_earlier:
                    skipped_not_earlier += 1
                    continue

            used[chosen_source] += 1
            years[chosen.year] += 1
            if not dry:
                client.client_added_at = chosen
                batch.append(client)
                if len(batch) >= BATCH:
                    Client.objects.bulk_update(batch, ["client_added_at"])
                    written += len(batch)
                    batch = []
                    self.stdout.write(f"  {written} written…")

        if batch and not dry:
            Client.objects.bulk_update(batch, ["client_added_at"])
            written += len(batch)

        total = sum(used.values())
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Source used"))
        for name in CHAIN:
            weak = "" if name in ACTIVITY_LEVELS else "   (weak evidence)"
            self.stdout.write(f"  {name:<24} {used[name]:>7}{weak}")
        self.stdout.write(f"  {'TOTAL':<24} {total:>7}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Resulting year spread"))
        for year in sorted(years):
            self.stdout.write(f"  {year}  {years[year]:>7}")

        self.stdout.write("")
        self.stdout.write(f"  no evidence (left as-is)      : {skipped_no_evidence}")
        self.stdout.write(f"  derived date is LATER than now: {would_move_forward}")
        if only_earlier:
            self.stdout.write(f"  skipped by --only-earlier      : {skipped_not_earlier}")
        if better_available:
            self.stdout.write(
                f"  a later source held an EARLIER date for {better_available} client(s) "
                "(the chain order picked the lifecycle-first source anyway)"
            )

        self.stdout.write("")
        if dry:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — nothing written. {total} client(s) would be updated."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated {written} client(s)."))
            self.stdout.write(
                "Now rebuild the Data page read model so the new dates show up:\n"
                "  python manage.py rebuild_enrollment_analytics --prune"
            )
