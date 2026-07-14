"""Delete tickets created on a given calendar date.

Works around the Django admin's bulk-delete limit (a "Bad Request (400)" from
DATA_UPLOAD_MAX_NUMBER_FIELDS when selecting >1000 rows). Deleting a Ticket
cascades its TicketNote rows.

Dry-run by default -- it prints how many tickets match and does nothing until
you pass --yes:

    python manage.py delete_tickets_by_date 2026-07-14           # preview only
    python manage.py delete_tickets_by_date 2026-07-14 --yes     # actually delete

The date is matched against created_at in the project's timezone (settings.TIME_ZONE),
so it lines up with the date shown in the admin. Optional filters:

    --status open|in_progress|resolved   restrict to one status
    --origin system|agent                restrict to system- or agent-raised
"""

from datetime import date as date_cls

from django.core.management.base import BaseCommand, CommandError

from api.models import Ticket


class Command(BaseCommand):
    help = "Delete tickets created on a given date (dry-run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "date", type=str, help="Calendar date, YYYY-MM-DD (e.g. 2026-07-14)."
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually delete. Without this the command only reports the count.",
        )
        parser.add_argument(
            "--status", type=str, default=None,
            help="Only tickets with this status (open/in_progress/resolved).",
        )
        parser.add_argument(
            "--origin", type=str, default=None,
            help="Only tickets with this origin (system/agent).",
        )

    def handle(self, *args, **options):
        raw = options["date"]
        try:
            target = date_cls.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"Invalid date {raw!r}; expected YYYY-MM-DD.")

        qs = Ticket.objects.filter(created_at__date=target)
        if options["status"]:
            qs = qs.filter(status=options["status"])
        if options["origin"]:
            qs = qs.filter(origin=options["origin"])

        count = qs.count()
        self.stdout.write(f"{count} ticket(s) created on {target.isoformat()} match.")
        if count == 0:
            return

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                "Dry run: nothing deleted. Re-run with --yes to delete them."
            ))
            return

        deleted, per_model = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} object(s) total: {per_model}"
        ))
