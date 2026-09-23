"""Classify existing programme holds into the hold-reason catalogue.

    python manage.py backfill_hold_reasons            # dry run
    python manage.py backfill_hold_reasons --apply

Two things are written:

  * every ``-> ON_HOLD`` StageEvent gets its category, so hold HISTORY is
    answerable -- "why was this household held in July" as well as "why now";
  * every enrollment CURRENTLY On Hold gets the category of its most recent hold.

HOW EACH HOLD IS CLASSIFIED, by the note the writing code left behind. The notes are
machine-written for every path except the agent one, so they are a reliable key --
unlike the member pause notes, which were free text throughout.

  Auto-paused: sole ... case denied        -> Governing Case Denied
  Auto-paused: last open ... case closed   -> Governing Case Closed
  all household members paused             -> All household members paused
  delivery ZIP outside coverage area       -> Delivery ZIP outside coverage
  social care coverage expired/missing     -> Social care coverage expired/missing
  member marked Ineligible by import       -> SPLIT by the client's stored reasons
  Roster import + "Pending Closure"        -> Pending Case Closure
  Placed on hold by <agent>. Reason: ...   -> keyword-matched, else Uncategorized

⚠ THE INELIGIBLE NOTE COVERS FOUR GATES -- expired insurance, missing insurance, an
unserved Medicaid plan type and an out-of-coverage ZIP -- so it cannot be classified
from the note at all. ``reason_for_ineligibility`` reads Client.ineligible_reasons
instead; on the clone that separates 16,898 Medicaid-type from 3,622 insurance and
3,122 ZIP.

⚠ THESE STAY UNCATEGORIZED BY DECISION, not by failure -- agreed rather than guessed:
the cancelled-enrollment reconcile (635), the bulk operations (672), the
carried-over-from-a-paused-household holds (592), and the roster's "Reason Unknown
(Potentially Authorization Status)" (134). Over-interpreting a July spreadsheet's
wording is how a category stops meaning anything.

Idempotent. Re-running without --overwrite skips anything already classified.
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

# (code, note patterns). ORDER MATTERS: the first match wins, so the specific
# machine-written notes precede the agent keyword matching.
NOTE_RULES = [
    ("governing_case_denied", (r"sole internal-service meal/box case denied",)),
    ("governing_case_closed", (r"last open internal-service meal/box case closed",)),
    ("all_members_paused", (r"all household members paused",
                            r"All members Nutritionist Paused")),
    # Anchored on the MACHINE note only. A bare /Out of Range/ also matched agent
    # free text -- same verdict, but reported as "machine-written", and it would
    # equally have matched an agent writing "not out of range". Agent wording belongs
    # in AGENT_RULES, where it is labelled honestly.
    ("zip_out_of_coverage", (r"^Automatically placed on hold.*outside coverage area",)),
    ("social_coverage_invalid", (r"social care coverage",)),
    # Decided, not guessed. See the module docstring.
    ("uncategorized", (r"^Cancelled reconcile", r"^Bulk hold", r"^Bulk pause",
                       r"^Kept On Hold", r"Reason Unknown")),
    ("pending_case_closure", (r"Roster import.*Pending Closure",)),
]

# The agent's free text, applied ONLY to "Placed on hold by ...". 1,556 distinct
# reasons across 1,796 current holds, so most land in Uncategorized and should.
AGENT_RULES = [
    ("pending_case_closure", (r"\bclos", r"disenroll", r"terminat")),
    ("governing_case_denied", (r"denied", r"denial")),
    ("insurance_invalid", (r"insur", r"medicaid", r"\bmltc\b", r"\bffs\b")),
    ("wrong_case_type", (r"wrong", r"duplicat", r"incorrect")),
    ("member_requested", (r"member (want|request|ask)", r"requested by member",
                          r"refus", r"declin", r"no longer want",
                          r"too m(uch|any)", r"out of town", r"vacation",
                          r"travel")),
    ("zip_out_of_coverage", (r"out of (range|area)", r"\bzip\b")),
]


class Command(BaseCommand):
    help = "Classify existing programme holds into the hold-reason catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without it, only report.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Re-classify holds that already have a reason.")

    def handle(self, *args, **options):
        from collections import Counter

        from api.models import (
            EnrollmentStage, EnrollmentVerification, HoldReason, StageEvent,
        )

        apply_it, overwrite = options["apply"], options["overwrite"]
        reasons = {r.code: r for r in HoldReason.objects.all()}
        needed = {c for c, _p in NOTE_RULES} | {c for c, _p in AGENT_RULES} | {
            "medicaid_type_not_served", "uncategorized",
        }
        missing = needed - set(reasons)
        if missing:
            self.stderr.write(self.style.ERROR(
                f"catalogue is missing: {', '.join(sorted(missing))}. Run migrate."
            ))
            return

        events = list(
            StageEvent.objects.filter(to_stage=EnrollmentStage.ON_HOLD)
            .select_related("enrollment", "client")
            .order_by("entered_at")
        )
        decided, how_counts, examples = Counter(), Counter(), {}
        to_write = []
        for event in events:
            if event.hold_reason_id and not overwrite:
                decided["(already set, skipped)"] += 1
                continue
            code, how = self._classify(event)
            decided[reasons[code].label] += 1
            how_counts[how] += 1
            examples.setdefault(
                (reasons[code].label, how), (event.note or "")[:52],
            )
            to_write.append((event, reasons[code]))

        self.stdout.write(f"'-> On Hold' stage events: {len(events):,}")
        self.stdout.write("")
        self.stdout.write("CATEGORY:")
        for label, n in decided.most_common():
            self.stdout.write(f"   {n:>6,}  {label}")
        self.stdout.write("")
        self.stdout.write("DECIDED BY:")
        for how, n in how_counts.most_common():
            self.stdout.write(f"   {n:>6,}  {how}")
        self.stdout.write("")
        self.stdout.write("examples:")
        for (label, how), note in sorted(examples.items())[:14]:
            self.stdout.write(f"   {label:34} [{how}] {note}")

        held = EnrollmentVerification.objects.filter(
            stage=EnrollmentStage.ON_HOLD,
        ).count()
        self.stdout.write("")
        self.stdout.write(f"enrollments currently On Hold: {held:,}")

        if not apply_it:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            for event, reason in to_write:
                event.hold_reason = reason
                event.save(update_fields=["hold_reason"])

        # The enrollment carries the CURRENT reason: its most recent hold. Done
        # after the events so it reads the classified values back.
        stamped = 0
        with transaction.atomic():
            for enrollment in EnrollmentVerification.objects.filter(
                stage=EnrollmentStage.ON_HOLD,
            ).iterator(chunk_size=500):
                if enrollment.hold_reason_id and not overwrite:
                    continue
                latest = (
                    StageEvent.objects.filter(
                        enrollment=enrollment, to_stage=EnrollmentStage.ON_HOLD,
                    ).order_by("-entered_at").first()
                )
                reason = getattr(latest, "hold_reason", None) or reasons[
                    "uncategorized"
                ]
                enrollment.hold_reason = reason
                enrollment.save(update_fields=["hold_reason"])
                stamped += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nclassified {len(to_write):,} hold events · "
            f"stamped {stamped:,} held enrollments"
        ))

    def _classify(self, event):
        """``(code, how)`` for one hold event."""
        from api.services.hold_reasons import reason_for_ineligibility

        note = (event.note or "").strip()
        if not note:
            return "uncategorized", "no note"

        # ⚠ FIRST, because the note cannot distinguish the four gates behind it.
        if "marked Ineligible by import" in note:
            client = event.client
            if client is None:
                return "uncategorized", "ineligible, no client"
            code = reason_for_ineligibility(client)
            return code, "ineligible + stored reasons"

        for code, patterns in NOTE_RULES:
            if any(re.search(p, note, re.I) for p in patterns):
                return code, "machine-written note"

        if note.startswith("Placed on hold by"):
            text = (
                note.split("Reason:", 1)[1].strip().lower()
                if "Reason:" in note else ""
            )
            for code, patterns in AGENT_RULES:
                if any(re.search(p, text) for p in patterns):
                    return code, "agent free text"
            return "uncategorized", "agent free text matched nothing"

        if note.startswith("Roster import"):
            return "uncategorized", "roster import, reason not mappable"
        return "uncategorized", "note matched nothing"
