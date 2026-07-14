"""Delete timeline events written (created) on a given calendar date.

Use this to trim the flood of ``TimelineEvent`` rows a bad/oversized import
wrote, without the Django admin's bulk-delete limit ("Bad Request (400)" from
DATA_UPLOAD_MAX_NUMBER_FIELDS when selecting >1000 rows).

Matches on ``created_at`` (the INSERTION time -- when the import wrote the row),
NOT ``occurred_at`` (the domain date shown on the row), so you delete exactly
what a given run created. Dry-run by default:

    python manage.py delete_timeline_by_date 2026-07-14                 # preview
    python manage.py delete_timeline_by_date 2026-07-14 --source import # scope to import rows
    python manage.py delete_timeline_by_date 2026-07-14 --yes           # actually delete

The date is matched in the project timezone (settings.TIME_ZONE). Deleting in
batches keeps the transaction / RDS load reasonable on very large sets.
"""

from datetime import date as date_cls

from django.core.management.base import BaseCommand, CommandError

from api.models import TimelineEvent


class Command(BaseCommand):
    help = "Delete timeline events created on a given date (dry-run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "date", type=str, help="Calendar date, YYYY-MM-DD (e.g. 2026-07-14)."
        )
        parser.add_argument(
            "--source", type=str, default=None,
            help="Only events with this source (import/extension/admin/crm/system).",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually delete. Without this the command only reports the count.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=5000,
            help="Rows to delete per batch when --yes (default 5000).",
        )

    def handle(self, *args, **options):
        raw = options["date"]
        try:
            target = date_cls.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"Invalid date {raw!r}; expected YYYY-MM-DD.")

        qs = TimelineEvent.objects.filter(created_at__date=target)
        if options["source"]:
            qs = qs.filter(source=options["source"])

        count = qs.count()
        scope = f" source={options['source']}" if options["source"] else ""
        self.stdout.write(
            f"{count} timeline event(s) created on {target.isoformat()}{scope} match."
        )
        if count == 0:
            return

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                "Dry run: nothing deleted. Re-run with --yes to delete them."
            ))
            return

        # Delete in batches so a huge set doesn't build one giant transaction.
        batch = max(1, options["batch_size"])
        deleted_total = 0
        while True:
            ids = list(qs.values_list("pk", flat=True)[:batch])
            if not ids:
                break
            n, _ = TimelineEvent.objects.filter(pk__in=ids).delete()
            deleted_total += n
            self.stdout.write(f"  deleted {deleted_total}/{count} ...")

        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_total} timeline event(s)."))
