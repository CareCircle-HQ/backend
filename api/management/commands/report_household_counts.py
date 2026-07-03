"""Report clients with an internal-service case that looks multi-person.

For every client that has at least one INTERNAL_SERVICE case (any status /
authorization), we flag them when EITHER signal says the household is bigger
than one person:

  * ANSWER  -- the TOTAL household-count answer on their assessments +
    screenings is >= the threshold. We use the SAME question phrasings the
    extension matches (see ``TOTAL_HOUSEHOLD_QUESTION_PATTERNS`` /
    ``eformTotalFamilyCount`` in ``extension/sidepanel/sidepanel.js``).
  * PROGRAM -- the internal-service case's ``program_name`` contains the word
    "household".

For each matched client we print:

    client_id | name | household_from_answer | current_household_members | internal_cases | match

``household_from_answer`` is the count the client reported on the questionnaire
(``-`` when not answered); ``current_household_members`` is how many
HouseholdMember rows currently exist in their household (0 = no household group
set up yet); ``match`` shows which signal(s) fired (answer/program). This is a
read-only report -- nothing is written.

Usage:
    python manage.py report_household_counts
    python manage.py report_household_counts --min 2   # threshold (default 2, i.e. > 1)
"""

import re

from django.core.management.base import BaseCommand
from django.db.models import Count

from api.models import Assessment, Case, CaseType, Client, Screening

# Spelled-out numbers we accept in an answer ("three" -> 3).
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Known phrasings of the TOTAL household-count question, most specific first --
# mirrors TOTAL_HOUSEHOLD_QUESTION_PATTERNS in the extension. The medicaid-
# enrolled variant is a different (subset) question and is excluded below.
_TOTAL_HOUSEHOLD_PATTERNS = [
    re.compile(r"how many\b.*\bimmediate family members?\b.*\bhousehold\b", re.I),
    re.compile(r"how many\b.*\bfamily members?\b.*\bhousehold\b", re.I),
    re.compile(r"how many\b.*\bpeople\b.*\bhousehold\b.*\bfood assistance\b", re.I),
    re.compile(r"how many\b.*\bpeople\b.*\bhousehold\b", re.I),
    re.compile(r"how many\b.*\bhousehold members?\b", re.I),
]


def _parse_count(answer):
    """Parse a count from an answer: a digit ("3", "3 members") or a spelled-out
    number ("three"). None if neither is present."""
    s = str(answer if answer is not None else "")
    m = re.search(r"\d+", s)
    if m:
        return int(m.group())
    w = re.search(r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten)\b", s.lower())
    return _NUM_WORDS[w.group(1)] if w else None


def _household_answer_count(items):
    """Return the TOTAL household count from ``items`` (list of (question,
    answer)), matched in pattern-priority order. Skips the medicaid-enrolled
    subset question. None if nothing matched with a parseable count."""
    for pattern in _TOTAL_HOUSEHOLD_PATTERNS:
        for question, answer in items:
            if "medicaid" in question.lower():
                continue  # medicaid-enrolled subset is a different question
            if pattern.search(question):
                n = _parse_count(answer)
                if n is not None:
                    return n
    return None


class Command(BaseCommand):
    help = (
        "Report clients with an internal-service case whose household-count "
        "answer (from assessments/screenings) is greater than the threshold."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--min", type=int, default=2,
            help="Minimum household-answer count to include (default 2, i.e. > 1).",
        )

    def handle(self, *args, **options):
        threshold = options["min"]

        # Clients with >=1 internal-service case (any status/authorization).
        internal = (
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .values("client_id")
            .annotate(n=Count("case_id"))
        )
        case_counts = {row["client_id"]: row["n"] for row in internal if row["client_id"]}

        # Collect the internal-service case program_names per client so we can
        # flag any whose program name mentions "household".
        program_names = {}
        for cid, pname in (
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(program_name="")
            .values_list("client_id", "program_name")
        ):
            if cid:
                program_names.setdefault(cid, set()).add(pname)

        clients = (
            Client.objects.filter(pk__in=case_counts.keys())
            .prefetch_related("assessments", "screenings", "household_membership__household__members")
        )

        rows = []
        for client in clients:
            items = []
            for a in client.assessments.all():
                for qa in (a.questions_answers or []):
                    items.append((qa.get("question") or "", qa.get("answer")))
            for s in client.screenings.all():
                for qa in (s.questions_answers or []):
                    items.append((qa.get("question") or "", qa.get("answer")))

            answer_count = _household_answer_count(items)
            answer_match = answer_count is not None and answer_count >= threshold
            program_match = any(
                "household" in p.lower() for p in program_names.get(client.pk, ())
            )
            if not (answer_match or program_match):
                continue

            membership = getattr(client, "household_membership", None)
            household = getattr(membership, "household", None)
            member_count = household.members.count() if household else 0

            match = "+".join(
                m for m, ok in (("answer", answer_match), ("program", program_match)) if ok
            )
            name = f"{(client.first_name or '').strip()} {(client.last_name or '').strip()}".strip()
            rows.append((
                str(client.pk), name, answer_count, member_count,
                case_counts[client.pk], match,
            ))

        # Sort by reported answer (None last), then by name.
        rows.sort(key=lambda r: (r[2] is None, -(r[2] or 0), r[1].lower()))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Internal-service clients that look multi-person "
            f"(answer > {threshold - 1} or program mentions 'household') ==="
        ))
        self.stdout.write(
            f"{'client_id':<38} {'name':<28} {'answer':>6} {'members':>8} {'cases':>6}  match"
        )
        for cid, name, answer_count, member_count, cases, match in rows:
            answer_str = "-" if answer_count is None else str(answer_count)
            self.stdout.write(
                f"{cid:<38} {name[:28]:<28} {answer_str:>6} {member_count:>8} {cases:>6}  {match}"
            )
        self.stdout.write(self.style.SUCCESS(f"\nTOTAL matched clients: {len(rows)}"))
