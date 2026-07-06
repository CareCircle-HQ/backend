"""One-off: force a roster of members to PENDING_VERIFICATION ("Needs
Verification"), from the FFF-NeedsVerification.xlsx sheet.

Per client this:

  1. **Ensures the household has an enrollment at PENDING_VERIFICATION** so the
     member shows up as needing verification (drives the client's lifecycle to
     Pending Verification and, cascading through the household, every member):
       * no enrollment  -> create one at PENDING_VERIFICATION;
       * already Pending Verification -> left as-is;
       * any OTHER stage (Verified / Kitchen Assignment / Service Active / ...)
         -> REGRESSED to PENDING_VERIFICATION. Because that is not a legal
         forward transition, the stage is set directly (not via
         ``advance_enrollment``), ``verified_at``/``verified_by`` are cleared
         (they must be re-verified), a StageEvent is written for audit, and the
         household lifecycle is recomputed.

  2. **Cancels existing scheduled deliveries** for any regressed member that had
     them (previously Service Active), so they drop off every Purchase Order --
     regressing the stage alone does NOT remove them from POs (PO generation
     doesn't gate on Service Active). OrderSchedule + MemberDeliverySchedule rows
     are set to Cancelled.

  3. **Updates the delivery address** from the sheet. This roster carries no
     meal info, so no dietary profile is written and no ticket is created.

Dry-run unless --apply; --force is required to COMMIT when warnings exist
(members regressed OUT of Service Active / Kitchen Assignment -- a destructive
change that pulls them out of service).

Usage:
    python manage.py mark_needs_verification_from_file                 # dry run
    python manage.py mark_needs_verification_from_file --apply --force  # commit
    python manage.py mark_needs_verification_from_file --file other.xlsx
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.management.commands.hold_pending_closure_from_file import _read_rows
from api.models import (
    Address,
    Client,
    EnrollmentStage,
    EnrollmentVerification,
    MemberDeliverySchedule,
    OrderSchedule,
    OrderStatus,
    ScheduleStatus,
    StageEntityType,
    StageEvent,
    StageEventSource,
)
from api.serializers import ensure_household_with_primary
from api.services.lifecycle import recompute_enrollment_household

_DEFAULT_FILE = "tmp/verification/FFF-NeedsVerification.xlsx"

# Stages that are AHEAD of verification -- regressing one of these is a
# destructive change (pulls the household out of service), so it triggers a
# commit warning.
_AHEAD_OF_VERIFICATION = {
    EnrollmentStage.VERIFIED,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
}


class Command(BaseCommand):
    help = (
        "Force a roster of members to Pending Verification (Needs Verification), "
        "cancelling schedules for any regressed-from-Active member and updating "
        "address. Dry-run unless --apply; --force to commit past warnings."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE)
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--force", action="store_true",
            help="Required with --apply to COMMIT when warnings exist.",
        )

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]
        force = options["force"]

        rows = _read_rows(path)
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Needs Verification roster: {path} -> {len(rows)} client rows"
        ))

        report = Counter()
        self.missing = []
        self.regressed_from_service = []   # (client_id, from_stage) -- warnings
        self.blocked = False

        with transaction.atomic():
            for rec in rows:
                try:
                    with transaction.atomic():
                        bucket = self._process(rec)
                except Exception as exc:
                    bucket = "error"
                    self.missing.append(f"{rec['id']} (error: {exc})")
                report[bucket] += 1

            has_warnings = bool(self.regressed_from_service)
            if not apply:
                transaction.set_rollback(True)
            elif has_warnings and not force:
                transaction.set_rollback(True)
                self.blocked = True

        self._report(report, apply, force, len(rows))

    def _process(self, rec):
        client = Client.objects.filter(client_id=rec["id"]).first()
        if client is None:
            self.missing.append(rec["id"])
            return "missing"

        household = ensure_household_with_primary(client)
        enr = household.enrollment_verifications.order_by("-opened_at").first()

        if enr is None:
            primary_hm = household.members.filter(is_primary=True).select_related("client").first()
            enr = EnrollmentVerification.objects.create(
                client=primary_hm.client if primary_hm else client,
                household=household,
                stage=EnrollmentStage.PENDING_VERIFICATION,
            )
            self._update_address(enr, rec)
            recompute_enrollment_household(enr)
            return "created"

        if enr.stage == EnrollmentStage.PENDING_VERIFICATION:
            self._update_address(enr, rec)
            return "already_pending"

        # Regress to PENDING_VERIFICATION (not a legal transition -> set directly).
        from_stage = EnrollmentStage(enr.stage)
        if from_stage in _AHEAD_OF_VERIFICATION:
            self.regressed_from_service.append((str(client.client_id), from_stage))

        # Cancel any live scheduled deliveries so the member drops off POs.
        cancelled_orders = OrderSchedule.objects.filter(
            enrollment=enr, status=OrderStatus.SCHEDULED
        ).update(status=OrderStatus.CANCELLED)
        MemberDeliverySchedule.objects.filter(
            enrollment=enr, status=ScheduleStatus.SCHEDULED
        ).update(status=ScheduleStatus.CANCELLED)
        if cancelled_orders:
            # tracked for the report
            self._last_cancelled = cancelled_orders

        now = timezone.now()
        enr.stage = EnrollmentStage.PENDING_VERIFICATION
        enr.stage_at = now
        enr.verified_at = None
        enr.verified_by = None
        enr.save(update_fields=["stage", "stage_at", "verified_at", "verified_by"])

        StageEvent.objects.create(
            entity_type=StageEntityType.ENROLLMENT,
            enrollment=enr,
            client=enr.client,
            from_stage=from_stage,
            to_stage=EnrollmentStage.PENDING_VERIFICATION,
            source=StageEventSource.MANUAL,
            note="Needs Verification import: regressed to Pending Verification.",
        )

        self._update_address(enr, rec)
        recompute_enrollment_household(enr)
        return "regressed"

    def _update_address(self, enr, rec):
        if not any(rec[k] for k in ("street", "city", "state", "zip")):
            return
        address = Address.objects.create(
            client=enr.client, type="temporary",
            street=rec["street"], unit=rec["apt"], city=rec["city"],
            state=rec["state"], zip=rec["zip"], notes=rec["addr_notes"],
        )
        enr.delivery_address = address
        enr.save(update_fields=["delivery_address"])

    def _report(self, report, apply, force, total):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Summary ==="))
        self.stdout.write(self.style.SUCCESS(
            f"  Enrollment created (Pending Verif.)   : {report.get('created', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Regressed to Pending Verification     : {report.get('regressed', 0)}"
        ))
        self.stdout.write(
            f"  Already Pending Verification          : {report.get('already_pending', 0)}"
        )
        self.stdout.write(self.style.WARNING(
            f"  Missing from DB                       : {report.get('missing', 0)}"
        ))
        if report.get("error"):
            self.stdout.write(self.style.WARNING(
                f"  Errored                               : {report.get('error', 0)}"
            ))
        self.stdout.write(f"  {'TOTAL rows':<37}: {total}")

        if self.regressed_from_service:
            self.stdout.write(self.style.ERROR(
                f"\n!!! WARNING: {len(self.regressed_from_service)} member(s) "
                "regressed OUT of service (Verified/Kitchen Assignment/Active). "
                "Their schedules were cancelled and they were pulled off POs. !!!"
            ))
            for cid, st in self.regressed_from_service[:60]:
                self.stdout.write(f"  {cid}: was {st}")

        if self.missing:
            self.stdout.write(head(f"\nMissing / errored ({len(self.missing)}, up to 60):"))
            for cid in self.missing[:60]:
                self.stdout.write(f"  {cid}")

        if self.blocked:
            self.stdout.write(self.style.ERROR(
                "\nNOT APPLIED: rolled back because warnings exist. Review above, "
                "then re-run with --apply --force to commit."
            ))
        elif apply:
            self.stdout.write(self.style.SUCCESS(
                "\nAPPLIED (committed)" + (" [--force]" if force else "") + "."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))
