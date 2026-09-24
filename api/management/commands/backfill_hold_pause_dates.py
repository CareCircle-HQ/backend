"""Backfill paused_at / resumed_at and held_at / hold_resumed_at from history.

    python manage.py backfill_hold_pause_dates            # dry run
    python manage.py backfill_hold_pause_dates --apply

The new fields are stamped going forward by ``MemberDietaryProfile.save`` and
``advance_enrollment``. Existing rows have nothing, so the dates are recovered from
what was already recorded:

  HOLDS -- from StageEvent, which is an append-only audit log of every stage
  transition and therefore exact. 7,379 "-> On Hold" events with timestamps.

      held_at          the most recent "-> ON_HOLD" event, for an enrollment
                       currently On Hold
      hold_resumed_at  the most recent transition AWAY from On Hold, for an
                       enrollment no longer held

  PAUSES -- from TimelineEvent, which records every member pause and resume with
  its timestamp. 9,277 relevant rows:

      member_paused        4,407        member_unpaused    1,552
      out_of_orbit         2,149        service_resumed      614
      out_of_range           518
      nutritionist_paused     37

      paused_at    the most recent pause event at or before the current status
      resumed_at   the most recent resume event, when the member is not paused now

⚠ AN EARLIER VERSION OF THIS COMMAND CLAIMED resumed_at WAS UNRECOVERABLE. It read
``status_changed_at`` -- one timestamp that cannot tell a resume from any other status
change -- and recovered 1,000 dates from "Member unpaused" NOTES, leaving the rest
NULL on the grounds that "the data was never kept".

The data was kept. TimelineEvent has held it all along, in the same table this
codebase already writes pause and resume events to. The lesson is narrow and worth
keeping: "the data does not exist" is a claim about a schema, and it needs a query,
not an inference from the one field that happened to be in view.

Idempotent: re-running skips anything already stamped unless --overwrite.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Recover the hold/pause dates from StageEvent + status history."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without it, only report.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Re-stamp rows that already have a date.")

    def handle(self, *args, **options):
        from collections import Counter

        from api.models import (
            EnrollmentStage, EnrollmentVerification, MEMBER_PAUSE_TIMESTAMP_STATUSES,
            MemberDietaryProfile, StageEvent, TimelineEvent,
        )

        apply_it, overwrite = options["apply"], options["overwrite"]
        counts = Counter()

        # ── holds, from the StageEvent audit log ────────────────────────────
        enrollment_writes = []
        for enr in EnrollmentVerification.objects.all().iterator(chunk_size=1000):
            if (enr.held_at or enr.hold_resumed_at) and not overwrite:
                counts["enrollment: already stamped"] += 1
                continue
            events = list(
                StageEvent.objects.filter(enrollment=enr)
                .order_by("-entered_at")
                .values_list("to_stage", "from_stage", "entered_at")[:40]
            )
            if not events:
                counts["enrollment: no stage history"] += 1
                continue

            held_at = hold_resumed_at = None
            if enr.stage == EnrollmentStage.ON_HOLD:
                for to_stage, _from, when in events:
                    if to_stage == EnrollmentStage.ON_HOLD:
                        held_at = when
                        break
                counts["enrollment: held_at recovered" if held_at
                       else "enrollment: ON HOLD but no hold event"] += 1
            else:
                # The most recent transition AWAY from On Hold.
                for _to, from_stage, when in events:
                    if from_stage == EnrollmentStage.ON_HOLD:
                        hold_resumed_at = when
                        break
                if hold_resumed_at:
                    counts["enrollment: hold_resumed_at recovered"] += 1
                    # And the hold it ended, so the pair reads as a cycle.
                    for to_stage, _from, when in events:
                        if to_stage == EnrollmentStage.ON_HOLD and when <= hold_resumed_at:
                            held_at = when
                            break
                else:
                    counts["enrollment: never held"] += 1

            if held_at or hold_resumed_at:
                enr.held_at = held_at
                enr.hold_resumed_at = hold_resumed_at
                enrollment_writes.append(enr)

        # ── pauses, from the TimelineEvent history ─────────────────────────
        PAUSE_EVENTS = (
            "member_paused", "out_of_orbit", "out_of_range",
            "nutritionist_paused",
        )
        RESUME_EVENTS = ("member_unpaused", "service_resumed")

        # Read in ONE pass, newest last so the final write per client wins.
        latest_pause, latest_resume = {}, {}
        for client_id, event_type, when in (
            TimelineEvent.objects
            .filter(event_type__in=PAUSE_EVENTS + RESUME_EVENTS)
            .order_by("occurred_at")
            .values_list("client_id", "event_type", "occurred_at")
        ):
            if event_type in PAUSE_EVENTS:
                latest_pause[client_id] = when
            else:
                latest_resume[client_id] = when

        profile_writes = []
        for profile in MemberDietaryProfile.objects.all().iterator(chunk_size=1000):
            if (profile.paused_at or profile.resumed_at) and not overwrite:
                counts["profile: already stamped"] += 1
                continue
            paused_now = profile.status in MEMBER_PAUSE_TIMESTAMP_STATUSES
            pause_at = latest_pause.get(profile.client_id)
            resume_at = latest_resume.get(profile.client_id)

            if paused_now:
                # Currently paused: the pause event is the one that matters, and
                # any earlier resume ended a PREVIOUS cycle -- carrying it over
                # would make a paused member read as resumed.
                if pause_at is None:
                    # Fall back to the status change: the member is demonstrably
                    # paused, so this timestamp IS their pause even without an event.
                    pause_at = profile.status_changed_at
                    if pause_at:
                        counts["profile: paused_at from status_changed_at"] += 1
                    else:
                        counts["profile: paused, nothing to read"] += 1
                        continue
                else:
                    counts["profile: paused_at from the timeline"] += 1
                profile.paused_at = pause_at
                profile.resumed_at = None
                profile_writes.append(profile)
                continue

            # Not paused now. A resume event is exact; without one there is nothing
            # to say, and status_changed_at must NOT be used -- for a member who was
            # never paused it is simply their last status change.
            if resume_at is None:
                counts["profile: never paused"] += 1
                continue
            profile.resumed_at = resume_at
            # And the pause it ended, so the pair reads as one cycle.
            if pause_at is not None and pause_at <= resume_at:
                profile.paused_at = pause_at
                counts["profile: full pause/resume cycle recovered"] += 1
            else:
                counts["profile: resumed_at only"] += 1
            profile_writes.append(profile)

        self.stdout.write("ENROLLMENTS (holds):")
        for k, v in sorted(counts.items()):
            if k.startswith("enrollment"):
                self.stdout.write(f"   {v:>7,}  {k.split(': ', 1)[1]}")
        self.stdout.write("")
        self.stdout.write("MEMBER PROFILES (pauses):")
        for k, v in sorted(counts.items()):
            if k.startswith("profile"):
                self.stdout.write(f"   {v:>7,}  {k.split(': ', 1)[1]}")
        self.stdout.write("")
        self.stdout.write(
            f"would write: {len(enrollment_writes):,} enrollment(s) · "
            f"{len(profile_writes):,} profile(s)"
        )

        if not apply_it:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            EnrollmentVerification.objects.bulk_update(
                enrollment_writes, ["held_at", "hold_resumed_at"], batch_size=500,
            )
        # ⚠ bulk_update, NOT save(): MemberDietaryProfile.save() stamps these fields
        # off a STATUS CHANGE, and there is no status change here -- calling save()
        # would leave them untouched and silently do nothing.
        with transaction.atomic():
            MemberDietaryProfile.objects.bulk_update(
                profile_writes, ["paused_at", "resumed_at"], batch_size=500,
            )
        self.stdout.write(self.style.SUCCESS(
            f"\nstamped {len(enrollment_writes):,} enrollment(s) · "
            f"{len(profile_writes):,} profile(s)"
        ))
