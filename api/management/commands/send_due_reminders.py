"""Fire dispatch-visit reminders whose time has come.

Run frequently -- every 5 minutes -- because a 30-minute reminder sent 20 minutes
late is worse than useless: the vendor is already on the road.

Dry-runs by default, like the other state-changing commands here. Pass --apply.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.services import scheduling


class Command(BaseCommand):
    help = "Send dispatch reminders that are due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually send. Without this the command only reports.",
        )
        parser.add_argument(
            "--limit", type=int, default=200,
            help="Safety ceiling on one pass.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        limit = options["limit"]

        due = list(scheduling.due_reminders()[:limit])
        if not due:
            self.stdout.write("Nothing due.")
            return

        self.stdout.write(
            f"{len(due)} reminder(s) due{'' if apply else ' (dry run)'}:"
        )
        sent = 0
        for reminder in due:
            order = reminder.visit.dispatch_order
            member = f"{order.client.first_name} {order.client.last_name}".strip()
            when = timezone.localtime(reminder.visit.scheduled_for)
            self.stdout.write(
                f"  {reminder.audience:7} {reminder.kind:11} {member} "
                f"@ {when:%Y-%m-%d %H:%M %Z}"
            )
            if not apply:
                continue
            try:
                scheduling.fire_reminder(reminder)
                sent += 1
            except Exception as exc:
                # One bad reminder must not stop the rest of the pass -- the next
                # one may be a visit in 30 minutes.
                self.stderr.write(
                    f"    FAILED {reminder.reminder_id}: {exc.__class__.__name__}: {exc}"
                )

        if apply:
            self.stdout.write(self.style.SUCCESS(f"Sent {sent}."))
        else:
            self.stdout.write("Dry run -- pass --apply to send.")
