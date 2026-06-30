"""Import / refresh Unite Us agents from the Unite Us users export CSV.

Usage:
    python manage.py import_unite_us_agents --path tmp/verification/users_export_2026-06-30.csv
    python manage.py import_unite_us_agents --dry-run

These are Unite Us (Unite NYC / SCN) platform users -- the Met Council / network
staff who create cases in Unite Us. Their ``user_id`` matches ``Case.created_by_id``
and the list is used as the cases-import creator allowlist (Settings).

Upsert is keyed by ``user_id`` (unique), so re-running updates, never duplicates.

CSV columns used:
    user_id, employee_id, first_name, last_name, email_address, work_title,
    employee_status

The CareCircle team roster (``--roster``, an .xlsx with columns Name / Us? /
Originating Team) is matched BY NAME to classify each agent: roster matches get
that row's ``is_us`` (Us? == Yes) and ``originating_team``; everyone NOT on the
roster is treated as Met Council staff (is_us=False, originating_team=
"Met Council Team").
"""

import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import UniteUsAgent

MET_COUNCIL_TEAM = "Met Council Team"


def _norm(value):
    return (value or "").strip()


def _norm_name(value):
    """Normalize a person name for matching (collapse whitespace, lowercase)."""
    return " ".join((value or "").split()).lower()


def _load_roster(path, stderr):
    """Return {normalized_name: (is_us: bool, originating_team: str)} from the
    CareCircle roster .xlsx, or None if it can't be read."""
    if not path or not os.path.exists(path):
        return None
    try:
        import openpyxl
    except ImportError:
        stderr.write("openpyxl not installed; skipping roster classification.")
        return None
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    roster = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header: Name / Us? / Originating Team
        name = _norm_name(row[0] if len(row) > 0 else "")
        if not name:
            continue
        us_raw = _norm(row[1] if len(row) > 1 else "").lower()
        team = _norm(row[2] if len(row) > 2 else "") or MET_COUNCIL_TEAM
        # Normalize the roster's "Met Council" label to the single canonical
        # "Met Council Team" used for everyone off the roster.
        if team.lower().startswith("met council"):
            team = MET_COUNCIL_TEAM
        roster[name] = (us_raw.startswith("yes"), team)
    return roster


def _uuid_or_none(value):
    value = _norm(value)
    return value or None


class Command(BaseCommand):
    help = "Import/refresh Unite Us agents from the users export CSV (upsert by user_id)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=os.path.join(
                settings.BASE_DIR, "tmp", "verification", "users_export_2026-06-30.csv"
            ),
            help="Path to the Unite Us users export CSV.",
        )
        parser.add_argument(
            "--roster",
            default=os.path.join(
                settings.BASE_DIR,
                "tmp",
                "verification",
                "CareCircle - Current Team Roster.xlsx",
            ),
            help="Path to the CareCircle team roster .xlsx (Name / Us? / Originating Team).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to the database.",
        )

    def handle(self, *args, **options):
        path = options["path"]
        dry_run = options["dry_run"]
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f"CSV not found: {path}"))
            return

        roster = _load_roster(options.get("roster"), self.stderr)
        if roster is None:
            self.stdout.write(
                "No roster loaded -- defaulting all agents to Met Council Team."
            )
            roster = {}
        else:
            self.stdout.write(f"Loaded {len(roster)} roster names for matching.")

        created = updated = unchanged = skipped = 0
        us_count = met_council_count = 0
        seen = set()

        with open(path, "r", encoding="utf-8-sig") as f, transaction.atomic():
            for row in csv.DictReader(f):
                r = {(_norm(k).lower()): v for k, v in row.items()}
                user_id = _uuid_or_none(r.get("user_id"))
                if not user_id:
                    skipped += 1
                    continue
                if user_id in seen:
                    skipped += 1  # duplicate row in the CSV
                    continue
                seen.add(user_id)

                first = _norm(r.get("first_name"))
                last = _norm(r.get("last_name"))
                name = " ".join(p for p in [first, last] if p) or _norm(
                    r.get("email_address")
                )
                # Match the CareCircle roster by name to classify the agent;
                # anyone not on the roster is Met Council staff.
                is_us, team = roster.get(_norm_name(name), (False, MET_COUNCIL_TEAM))
                if is_us:
                    us_count += 1
                if team == MET_COUNCIL_TEAM:
                    met_council_count += 1
                fields = {
                    "employee_id": _uuid_or_none(r.get("employee_id")),
                    "first_name": first,
                    "last_name": last,
                    "name": name,
                    "email": _norm(r.get("email_address")).lower(),
                    "work_title": _norm(r.get("work_title")),
                    "status": _norm(r.get("employee_status")).lower() or "active",
                    "is_us": is_us,
                    "originating_team": team,
                }

                agent = UniteUsAgent.objects.filter(user_id=user_id).first()
                if agent is None:
                    created += 1
                    self.stdout.write(f"  + create  {name}  <{fields['email']}>")
                    if not dry_run:
                        UniteUsAgent.objects.create(user_id=user_id, **fields)
                else:
                    changes = {
                        k: v for k, v in fields.items()
                        if getattr(agent, k) != v
                    }
                    if changes:
                        updated += 1
                        self.stdout.write(
                            f"  ~ update  {name}  -> {', '.join(sorted(changes))}"
                        )
                        if not dry_run:
                            for k, v in changes.items():
                                setattr(agent, k, v)
                            agent.save(update_fields=list(changes.keys()) + ["updated_at"])
                    else:
                        unchanged += 1

            if dry_run:
                self.stdout.write(self.style.WARNING("DRY RUN — rolling back, no changes saved."))
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Unite Us agents import done: {created} created, {updated} updated, "
                f"{unchanged} unchanged, {skipped} skipped."
            )
        )
        self.stdout.write(
            f"Classification: {us_count} US (CareCircle), "
            f"{met_council_count} Met Council Team."
        )
