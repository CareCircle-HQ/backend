"""One-off migration: move members currently flagged **Out of Range** onto the
new **Not Eligible** off-ramp.

The delivery-coverage rule used to set a member's status to ``OUT_OF_RANGE`` and
place the household On Hold. It now mirrors the eligibility off-ramp instead: the
member is set Not Eligible (client lifecycle ``INELIGIBLE`` with the stable
"Delivery Address Outside Coverage Area" reason) and Paused. This command
converts every existing Out-of-Range member on a LIVE (non-terminal) enrollment
to that state, attaching the proper reason (with the offending ZIP when it can
still be determined).

Idempotent + safe to re-run. Previews by default; pass ``--apply`` to write.
"""

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Convert existing Out-of-Range members to the Not Eligible off-ramp."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write changes. Without this flag the command only previews (dry run).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything (default behaviour).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N clients (0 = all).",
        )

    def handle(self, *args, **opts):
        from api.models import EnrollmentStage, MemberDietaryProfile, MemberStatus
        from api.services.eligibility import apply_out_of_range_ineligibility
        from api.services.service_area import excluded_zips, member_excluded_info

        dry_run = not opts["apply"]
        limit = opts["limit"]
        terminal = [
            EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED,
            EnrollmentStage.DISREGARDED, EnrollmentStage.SERVICE_COMPLETE,
        ]
        profiles = (
            MemberDietaryProfile.objects
            .filter(status=MemberStatus.OUT_OF_RANGE)
            .exclude(enrollment__stage__in=[s.value for s in terminal])
            .select_related("client", "enrollment")
            .order_by("client_id")
        )
        excluded = excluded_zips()

        # Dedupe to one entry per client (apply_out_of_range_ineligibility pauses
        # ALL of a client's live profiles + sets the client Not Eligible once).
        seen = set()
        targets = []
        for mv in profiles:
            if not mv.client_id or mv.client_id in seen:
                continue
            seen.add(mv.client_id)
            zip_code, source = member_excluded_info(mv, excluded=excluded)
            targets.append((mv, zip_code, source))
        if limit:
            targets = targets[:limit]

        self.stdout.write(
            f"Found {len(targets)} client(s) with a live Out-of-Range member profile."
        )
        converted = 0
        for mv, zip_code, source in targets:
            client = mv.client
            name = f"{client.first_name or ''} {client.last_name or ''}".strip() or str(client.pk)
            reason = (
                f"The {source} ZIP {zip_code} is outside the delivery coverage area."
                if zip_code
                else "The delivery address is outside the delivery coverage area."
            )
            if dry_run:
                self.stdout.write(f"  [dry-run] {name} ({client.pk}) -> Not Eligible :: {reason}")
                continue
            with transaction.atomic():
                apply_out_of_range_ineligibility(
                    client, reason_detail=reason, actor_label="System (migration)",
                )
            converted += 1
            self.stdout.write(f"  {name} ({client.pk}) -> Not Eligible")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Converted {converted} client(s) to Not Eligible."))
