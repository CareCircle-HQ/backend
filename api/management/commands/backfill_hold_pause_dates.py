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

  PAUSES -- from ``status_changed_at``, which the model has always stamped on a
  status change.

      paused_at        status_changed_at, when the member is CURRENTLY paused
      resumed_at       left NULL -- see below

⚠ resumed_at CANNOT BE RECOVERED FOR PAST PAUSES, and is deliberately left NULL
rather than guessed. ``status_changed_at`` holds ONE timestamp: for a member who was
paused and is now active it is the resume, but for a member who has never been paused
it is simply their last status change, and the two are indistinguishable. Writing it
as a resume date would invent a pause that never happened for ~21,000 active members.

So a "was resumed between" filter returns nothing for historical pauses. That is
honest: the data to answer it was never kept. Forward-dated pauses answer it exactly.

There IS a fallback for members whose resume left a note -- "Member unpaused", 1,027
of them -- and those are recovered.

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
            MemberDietaryProfile, Note, StageEvent,
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

        # ── pauses, from status_changed_at ──────────────────────────────────
        # Resume notes, read once: the only recoverable resume signal.
        resume_notes = {}
        for client_id, body, created in Note.objects.filter(
            body__startswith="Member unpaused",
        ).order_by("created_at").values_list("client_id", "body", "created_at"):
            resume_notes[client_id] = created

        profile_writes = []
        for profile in MemberDietaryProfile.objects.all().iterator(chunk_size=1000):
            if (profile.paused_at or profile.resumed_at) and not overwrite:
                counts["profile: already stamped"] += 1
                continue
            paused = profile.status in MEMBER_PAUSE_TIMESTAMP_STATUSES
            if paused:
                if profile.status_changed_at:
                    profile.paused_at = profile.status_changed_at
                    profile.resumed_at = None
                    profile_writes.append(profile)
                    counts["profile: paused_at recovered"] += 1
                else:
                    counts["profile: paused, no status_changed_at"] += 1
                continue
            # ⚠ NOT paused now. status_changed_at cannot tell a resume from any
            # other status change, so only a resume NOTE is trusted.
            when = resume_notes.get(profile.client_id)
            if when is not None:
                profile.resumed_at = when
                profile_writes.append(profile)
                counts["profile: resumed_at from a note"] += 1
            else:
                counts["profile: not paused, no resume note"] += 1

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
