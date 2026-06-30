"""Import / refresh agents from the company directory CSV.

Usage:
    python manage.py import_agents
    python manage.py import_agents --path tmp/users_agents.csv
    python manage.py import_agents --dry-run

CSV columns (from the company directory export):
    Display name, User principal name, First name, Last name, Title, Department

Matching is by EMAIL (case-insensitive). An existing agent keeps its
``agent_code`` (and other dialer identity fields) — only directory fields
(name, first/last name, title, department, email) are refreshed. Agents not
already in the table are created without an ``agent_code`` (they can still log
in via email + 2FA, but CallTools stays disabled until a code is assigned).

All emails are stored lowercased. A final pass also lowercases any existing
agent emails not present in the CSV, so the whole table is normalized.

Safe to re-run after editing the CSV. Use ``--dry-run`` to preview changes.
"""

import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Agent


def _norm(key):
    return (key or "").strip().lower()


class Command(BaseCommand):
    help = "Import/refresh agents from the company directory CSV (matches by email, preserves agent_code)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=os.path.join(settings.BASE_DIR, "tmp", "users_agents.csv"),
            help="Path to the CSV file (default: tmp/users_agents.csv).",
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

        created = updated = unchanged = skipped = 0
        seen_emails = set()

        with open(path, "r", encoding="utf-8-sig") as f, transaction.atomic():
            for row in csv.DictReader(f):
                r = {_norm(k): (v or "").strip() for k, v in row.items()}
                # Email column varies by export: the directory uses "User
                # principal name"; the Azure user export uses "Email Address".
                email = _norm(
                    r.get("user principal name")
                    or r.get("email address")
                    or r.get("email")
                )
                if not email or "@" not in email:
                    skipped += 1
                    continue
                if email in seen_emails:
                    skipped += 1  # duplicate row in the CSV
                    continue
                seen_emails.add(email)

                name = (
                    r.get("display name")
                    or " ".join(
                        p for p in [r.get("first name"), r.get("last name")] if p
                    )
                    or email
                )
                directory_fields = {
                    "name": name,
                    "first_name": r.get("first name", ""),
                    "last_name": r.get("last name", ""),
                    "title": r.get("title", ""),
                    "department": r.get("department", ""),
                }

                # Match by email (case-insensitive). Preserve agent_code and all
                # other dialer-identity fields — only refresh directory fields.
                agent = Agent.objects.filter(email__iexact=email).first()
                if agent is None:
                    created += 1
                    self.stdout.write(f"  + create  {email}  ({name})")
                    if not dry_run:
                        Agent.objects.create(email=email, **directory_fields)
                else:
                    changes = {
                        k: v for k, v in directory_fields.items()
                        if getattr(agent, k) != v
                    }
                    if agent.email != email:
                        changes["email"] = email  # normalize casing
                    if changes:
                        updated += 1
                        self.stdout.write(
                            f"  ~ update  {email}  (code={agent.agent_code or '-'}) "
                            f"-> {', '.join(sorted(changes))}"
                        )
                        if not dry_run:
                            for k, v in changes.items():
                                setattr(agent, k, v)
                            agent.save(update_fields=list(changes.keys()) + ["updated_at"])
                    else:
                        unchanged += 1

            # Normalize any remaining non-lowercase emails table-wide.
            lowered = 0
            for agent in Agent.objects.exclude(email=""):
                low = agent.email.lower()
                if agent.email != low:
                    lowered += 1
                    if not dry_run:
                        agent.email = low
                        agent.save(update_fields=["email", "updated_at"])

            if dry_run:
                self.stdout.write(self.style.WARNING("DRY RUN — rolling back, no changes saved."))
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Agent import done: {created} created, {updated} updated, "
                f"{unchanged} unchanged, {skipped} skipped, "
                f"{lowered} emails lowercased."
            )
        )
