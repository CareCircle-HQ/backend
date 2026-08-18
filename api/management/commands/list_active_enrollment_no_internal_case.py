"""Read-only diagnostic: list members who hold an ACTIVE (live) enrollment but
have NO internal-service (meal/box) case at all.

Every serving member should be governed by an internal-service case, so an active
enrollment with no such case is a data anomaly (e.g. a caseless enrollment left
behind by a governing-case switch). Prints the client_id (+ name + stage); makes
NO changes.

"Active" = a non-terminal, non-parked enrollment stage. Pass --service-active-only
to restrict to stage = Service Active. "No internal-service case at all" means the
enrollment's client holds zero INTERNAL_SERVICE cases of ANY status.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from api.models import Case, CaseType, EnrollmentStage, EnrollmentVerification

# Stages that are NOT a live/active enrollment.
_NON_ACTIVE_STAGES = [
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    EnrollmentStage.DISREGARDED,
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.SCHEDULED_EXTENSION,
]


class Command(BaseCommand):
    help = (
        "Print members with an ACTIVE enrollment but no internal-service case "
        "(read-only, no changes)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--service-active-only", action="store_true",
            help="Only stage = Service Active (default: any live/non-terminal stage).",
        )

    def handle(self, *args, **opts):
        # An ACTIVE VERIFICATION enrollment: live (non-terminal) AND actually
        # verified (verified_at set) -- a completed verification still in service.
        enr = EnrollmentVerification.objects.filter(verified_at__isnull=False)
        if opts["service_active_only"]:
            enr = enr.filter(stage=EnrollmentStage.SERVICE_ACTIVE)
        else:
            enr = enr.exclude(stage__in=_NON_ACTIVE_STAGES)

        # The enrollment's client holds NO internal-service case (any status).
        has_internal = Case.objects.filter(
            client=OuterRef("client_id"), case_type=CaseType.INTERNAL_SERVICE,
        )
        rows = (
            enr.exclude(Exists(has_internal))
            .select_related("client")
            .order_by("client__last_name", "client__first_name")
        )

        seen = set()
        header = f"{'client_id':<38}{'stage':<20}name"
        self.stdout.write(header)
        for e in rows.iterator(chunk_size=1000):
            cid = str(e.client_id)
            if cid in seen:
                continue
            seen.add(cid)
            c = e.client
            name = f"{(c.first_name or '').strip()} {(c.last_name or '').strip()}".strip() if c else ""
            self.stdout.write(f"{cid:<38}{e.stage:<20}{name}")

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(seen)} member(s) with an active enrollment and NO "
            f"internal-service case."
        ))
