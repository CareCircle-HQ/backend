"""Classify existing pauses into the pause-reason catalogue.

    python manage.py backfill_pause_reasons            # dry run
    python manage.py backfill_pause_reasons --apply

Every member currently in a non-serving status gets a reason. Where one can be read
off the data it is used; where it cannot, ``Uncategorized`` -- which is findable,
unlike a NULL.

HOW EACH REASON IS DECIDED, strongest evidence first:

  1  THE STATUS itself, for the three that ARE reasons:
         OUT_OF_ORBIT         -> Out of Orbit
         OUT_OF_RANGE         -> Out of Range
         NUTRITIONIST_PAUSED  -> Nutritionist Paused
     No text needed and none consulted: the status is the fact.

  2  THE PROFILE FLAGS, for the two the system owns:
         pause_locked=True       -> Case Type Switch
         eligibility_paused=True -> Insurance expired or invalid, but ONLY when the
                                    client's stored ineligible_reasons mention
                                    insurance. The gate has four failure modes and
                                    only one maps to a catalogue reason, so the rest
                                    stay Uncategorized rather than being mislabelled.

  3  THE PAUSE NOTE, for a manual agent pause. 1,341 notes, 200 distinct strings.

⚠ THE NOTE TEXT IS THE WEAKEST SIGNAL and is consulted LAST, because the largest
group in it is not a reason at all: "9/1 HH Close" appears 937 times -- 70% of every
pause note -- and is one bulk campaign, not a cause anybody chose. It is matched
explicitly and sent to Uncategorized rather than being allowed to invent a category
that would then dominate every report built on this field.

One note is a pasted UUID, and several describe the END of a pause ("will call to
resume") rather than its cause. A free-text box collects this; the catalogue exists
so it stops.
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

# Ordered: the FIRST match wins, so the specific patterns precede the loose ones.
# "insurance" before "cancel" matters -- "cancel policy" is an insurance problem.
NOTE_PATTERNS = [
    # The bulk campaign, FIRST and deliberately unclassified. Matching it here stops
    # it falling through to a keyword and manufacturing a category out of one day's
    # work.
    ("uncategorized", (r"^9/1\b", r"^9/30\b", r"^09/30\b", r"hh close", r"^9/1 hh")),
    ("insurance_invalid", (r"insuranc", r"medicaid", r"\bffs\b", r"coverage",
                           r"\bmltc\b", r"\bmap\b")),
    ("address_problem", (r"address", r"\bmoved\b", r"moving", r"relocat",
                         r"does not live")),
    ("case_type_switch", (r"individual case", r"hybrid", r"household case",
                          r"case switch", r"switched")),
    ("away_travelling", (r"out of town", r"travel", r"vacation", r"\baway\b",
                         r"abroad", r"overseas", r"\btrip\b", r"leaving for",
                         r"won'?t be home for")),
    ("too_much_food", (r"too m(uch|any)", r"to m(uch|any)", r"not being eaten",
                       r"has enough", r"\benough\b", r"excess")),
    ("not_home", (r"not (being )?home", r"no one home", r"nobody home")),
    ("delivery_issue", (r"deliver", r"driver", r"shipment", r"missed")),
    ("member_cancelled", (r"\bcancel", r"stop receiving", r"no longer want",
                          r"does ?n[o']?t (need|want|to receive)", r"terminat",
                          r"discontinu", r"opt(ed)? out")),
    ("pending_review", (r"pending", r"review", r"\bhold\b", r"waiting",
                        r"verify", r"until")),
]


class Command(BaseCommand):
    help = "Classify existing member pauses into the pause-reason catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it the command only reports.",
        )
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Re-classify profiles that already have a reason.",
        )
        parser.add_argument(
            "--reclassify-uncategorized", action="store_true",
            help=(
                "Re-run ONLY over profiles currently on Uncategorized. Safer than "
                "--overwrite, which would also revisit reasons an agent set by hand."
            ),
        )

    def handle(self, *args, **options):
        from collections import Counter

        from api.models import (
            MemberDietaryProfile, MemberStatus, Note, PauseReason,
        )

        apply_it = options["apply"]
        overwrite = options["overwrite"]
        # ⚠ NOT the same as --overwrite. This revisits ONLY the Uncategorized ones,
        # so a category an agent chose deliberately is never second-guessed by a
        # classifier -- which is the whole risk of re-running a backfill over live
        # data.
        redo_uncategorized = options["reclassify_uncategorized"]

        reasons = {r.code: r for r in PauseReason.objects.all()}
        missing = {
            code for code, _pats in NOTE_PATTERNS
        } | {"out_of_orbit", "out_of_range", "nutritionist_paused",
             "case_type_switch", "insurance_invalid", "uncategorized"}
        missing -= set(reasons)
        if missing:
            self.stderr.write(self.style.ERROR(
                f"catalogue is missing: {', '.join(sorted(missing))}. "
                "Run migrate first."
            ))
            return

        # STATUS -> reason, for the three statuses that ARE reasons.
        by_status = {
            MemberStatus.OUT_OF_ORBIT: "out_of_orbit",
            MemberStatus.OUT_OF_RANGE: "out_of_range",
            MemberStatus.NUTRITIONIST_PAUSED: "nutritionist_paused",
        }

        # ⚠ INACTIVE IS DELIBERATELY EXCLUDED. It is a terminal end state -- "their
        # service ended" -- not a pause, and it has no pause note. Including it put
        # 1,339 members into Uncategorized and made the field two-thirds noise, which
        # would have been the first thing anyone noticed about this feature.
        #
        # REMOVED is excluded for the same reason: it is history, kept on the old
        # enrollment after a household split.
        targets = (
            MemberStatus.PAUSED, MemberStatus.NUTRITIONIST_PAUSED,
            MemberStatus.OUT_OF_ORBIT, MemberStatus.OUT_OF_RANGE,
        )
        profiles = list(
            MemberDietaryProfile.objects
            .filter(status__in=targets)
            .select_related("client", "pause_reason")
        )

        # The most recent pause note per client, read in ONE query rather than per
        # profile -- 1,341 notes against ~3,500 profiles.
        notes = {}
        for client_id, body in Note.objects.filter(
            body__startswith="Member paused. Reason:",
        ).order_by("created_at").values_list("client_id", "body"):
            notes[client_id] = body.split("Reason:", 1)[1].strip()

        decided = Counter()
        source_of = Counter()
        examples = {}
        to_write = []
        for profile in profiles:
            already = profile.pause_reason_id is not None
            is_uncategorized = (
                already and profile.pause_reason
                and profile.pause_reason.code == "uncategorized"
            )
            if already and not overwrite and not (
                redo_uncategorized and is_uncategorized
            ):
                decided["(already set, skipped)"] += 1
                continue

            code, how = self._classify(profile, by_status, notes)
            decided[reasons[code].label] += 1
            source_of[how] += 1
            examples.setdefault((reasons[code].label, how), notes.get(
                profile.client_id, f"status={profile.status}",
            ))
            to_write.append((profile, reasons[code]))

        self.stdout.write(f"profiles in a non-serving status: {len(profiles):,}")
        self.stdout.write("")
        self.stdout.write("REASON:")
        for label, n in decided.most_common():
            self.stdout.write(f"   {n:>6,}  {label}")
        self.stdout.write("")
        self.stdout.write("DECIDED BY:")
        for how, n in source_of.most_common():
            self.stdout.write(f"   {n:>6,}  {how}")
        self.stdout.write("")
        self.stdout.write("examples:")
        for (label, how), text in sorted(examples.items())[:12]:
            self.stdout.write(f"   {label:30} [{how}] {text[:44]}")

        if not apply_it:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            for profile, reason in to_write:
                profile.pause_reason = reason
                profile.save(update_fields=["pause_reason"])
        self.stdout.write(self.style.SUCCESS(
            f"\nSet a reason on {len(to_write):,} profile(s)."
        ))

    def _classify(self, profile, by_status, notes):
        """``(code, how)`` for one profile. Strongest evidence first."""
        # 1. the status, where the status IS the reason
        code = by_status.get(profile.status)
        if code:
            return code, "status"

        # 2. the profile flags
        if profile.pause_locked:
            return "case_type_switch", "pause_locked flag"
        if profile.eligibility_paused:
            client = profile.client
            stored = " ".join(
                getattr(client, "ineligible_reasons", None) or []
            ).lower()
            # ⚠ MEDICAID PLAN TYPE IS AN INSURANCE FAILURE. Checked FIRST, because
            # the string contains "FFS" and sits beside insurance wording -- and
            # because it is the largest ineligibility gate in the system, 493 of the
            # 577 pauses that were sitting in Uncategorized.
            #
            # I originally left these Uncategorized on the grounds that calling an
            # unserved plan type "insurance" would be a guess. It is not a guess: the
            # member's Medicaid plan is the insurance, and an unserved plan type
            # makes it invalid for our purposes. Confirmed as the intended
            # categorisation.
            if "medicaid plan type" in stored:
                return "insurance_invalid", "eligibility gate, Medicaid plan type"
            if "insurance" in stored:
                return "insurance_invalid", "eligibility gate + stored reason"
            # A ZIP, ADDRESS or state failure IS Out of Range, even though the
            # member's STATUS is PAUSED rather than OUT_OF_RANGE -- the gate pauses
            # them individually instead of setting the status.
            #
            # ⚠ "outside THE coverage area" was too tight: the delivery-address
            # variant reads "delivery address outside coverage area", with no "the",
            # and slipped into Uncategorized.
            if "outside the coverage area" in stored or "outside coverage area" in stored:
                return "out_of_range", "eligibility gate, ZIP/address"
            if "is not served" in stored:
                return "out_of_range", "eligibility gate, state"
            return "uncategorized", "eligibility gate, no catalogue reason"

        # 3. the note text -- weakest, and only for a manual pause
        text = (notes.get(profile.client_id) or "").lower()
        if text:
            for code, patterns in NOTE_PATTERNS:
                if any(re.search(p, text) for p in patterns):
                    return code, "pause note"
            return "uncategorized", "note matched nothing"
        return "uncategorized", "no evidence at all"
