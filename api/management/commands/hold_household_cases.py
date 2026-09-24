"""Tag and hold every member whose GOVERNING case is an open household case.

    python manage.py hold_household_cases            # dry run
    python manage.py hold_household_cases --apply

⚠ THIS STOPS SERVICE FOR HUNDREDS OF HOUSEHOLDS. An On Hold enrollment is excluded
from delivery generation and from every future Purchase Order. Measured on the clone
before writing:

    open internal-service HOUSEHOLD cases      745
      ... whose member's GOVERNING case it is  729
      ... already On Hold                       82   no-op
      ... would actually change                646

WHAT IS IN SCOPE, and the three narrowings that are NOT obvious from the request:

  GOVERNING ONLY. 16 members hold an open household case that is not the one
  currently governing them; holding an enrollment on a case that is not in force
  would stop service for a reason that is not true.

  THE ENROLLMENT IS FOUND BY CASE, not by client. A member can have several
  enrollments, and the one to hold is the one for THIS case -- 727 of 729 match
  that way. The single client-matched fallback is reported so it can be eyeballed.

  CLOSED AND MISSING ENROLLMENTS ARE SKIPPED ENTIRELY -- not even tagged. Moving a
  closed enrollment to On Hold would REOPEN it, which is a different and much larger
  action than the one asked for. 7 closed and 1 absent.

Idempotent: a member already tagged and already On Hold is counted and left alone.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

TAG_NAME = "9/23 HH HOLD"
HOLD_NOTE = "Bulk hold 9/23: governing household case."

# The statuses that mean a case is in force. Matches the rest of the codebase.
LIVE_CASE_STATUSES = ("open", "managed", "pending_authorization", "off_platform")

# Enrollment stages we refuse to touch. Terminal, or nothing to hold.
SKIP_STAGES = {"closed"}


class Command(BaseCommand):
    help = "Tag + hold members whose governing case is an open household case."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it the command only reports.",
        )

    def handle(self, *args, **options):
        from collections import Counter

        from api.models import (
            CaseHouseholdType, Client, ClientTag, EnrollmentStage,
            EnrollmentVerification,
        )
        from api.portal.serializers import internal_service_case

        apply_it = options["apply"]

        clients = Client.objects.filter(
            cases__case_type="internal_service",
            cases__household_type=CaseHouseholdType.HOUSEHOLD,
            cases__case_status__in=LIVE_CASE_STATUSES,
        ).distinct().prefetch_related("cases", "tags")

        counts = Counter()
        work = []
        skipped = []
        for client in clients.iterator(chunk_size=500):
            governing = internal_service_case(client)
            if (
                governing is None
                or governing.household_type != CaseHouseholdType.HOUSEHOLD
                or governing.case_status not in LIVE_CASE_STATUSES
            ):
                counts["not their governing case"] += 1
                continue

            enrollment = (
                EnrollmentVerification.objects
                .filter(case=governing).order_by("-opened_at").first()
            )
            how = "by case"
            if enrollment is None:
                enrollment = (
                    EnrollmentVerification.objects
                    .filter(client=client).order_by("-opened_at").first()
                )
                how = "by client (fallback)"

            if enrollment is None:
                counts["SKIPPED: no enrollment"] += 1
                skipped.append((client, "no enrollment"))
                continue
            if enrollment.stage in SKIP_STAGES:
                counts["SKIPPED: enrollment closed"] += 1
                skipped.append((client, f"enrollment {enrollment.stage}"))
                continue

            counts[f"in scope ({how})"] += 1
            counts[f"  currently {enrollment.stage}"] += 1
            work.append((client, enrollment))

        tag = ClientTag.objects.filter(name=TAG_NAME).first()
        already_tagged = sum(
            1 for c, _e in work if any(t.name == TAG_NAME for t in c.tags.all())
        )
        to_hold = sum(
            1 for _c, e in work if e.stage != EnrollmentStage.ON_HOLD
        )

        self.stdout.write(f"members in scope: {len(work):,}")
        self.stdout.write("")
        for key, n in sorted(counts.items()):
            self.stdout.write(f"   {n:>5}  {key}")
        self.stdout.write("")
        self.stdout.write(f"   tag exists already : {tag is not None}")
        self.stdout.write(f"   already tagged     : {already_tagged:,}")
        self.stdout.write(f"   to TAG             : {len(work) - already_tagged:,}")
        self.stdout.write(f"   to HOLD            : {to_hold:,}")

        if skipped:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{len(skipped)} member(s) skipped ENTIRELY -- not tagged either, "
                "because holding a closed enrollment would reopen it:"
            ))
            for client, why in skipped[:15]:
                self.stdout.write(
                    f"    {client.first_name} {client.last_name}  ({why})"
                )

        if not apply_it:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write."
            ))
            return

        from api.services.lifecycle import InvalidTransition, advance_enrollment

        if tag is None:
            tag = ClientTag.objects.create(name=TAG_NAME)
            self.stdout.write(f'created the tag "{TAG_NAME}"')

        tagged = held = failed = 0
        problems = []
        for client, enrollment in work:
            # Tag and hold are committed PER MEMBER, not in one transaction over all
            # 729. A single bad transition should not roll back 700 good ones, and
            # a re-run picks up exactly where this left off.
            try:
                with transaction.atomic():
                    if not any(t.name == TAG_NAME for t in client.tags.all()):
                        client.tags.add(tag)
                        tagged += 1
                    if enrollment.stage != EnrollmentStage.ON_HOLD:
                        advance_enrollment(
                            enrollment, EnrollmentStage.ON_HOLD,
                            actor_label="System (bulk 9/23)",
                            note=HOLD_NOTE,
                            # force: several of these sit in stages with gates that
                            # a normal hold would refuse. The hold IS the intent, and
                            # the transition map is still validated.
                            force=True,
                            trigger="bulk_hh_hold_0923",
                        )
                        held += 1
            except (InvalidTransition, Exception) as exc:  # noqa: BLE001
                failed += 1
                problems.append((client, str(exc)[:90]))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"tagged {tagged:,} · held {held:,} · failed {failed:,}"
        ))
        if problems:
            self.stdout.write(self.style.WARNING("failures:"))
            for client, why in problems[:15]:
                self.stdout.write(
                    f"    {client.first_name} {client.last_name}: {why}"
                )
