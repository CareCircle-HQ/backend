"""Export enrollments at a given stage to CSV, one row per household member.

Produces two files by default:
  * kitchen_assignment_<date>.csv   -- stage = Kitchen Assignment
  * pending_verification_<date>.csv -- stage = Pending Verification

Each row is one participant (MemberDietaryProfile) of a qualifying enrollment;
enrollment-level columns (delivery address, kitchen, cadence, case
authorization, program) are repeated for every member of the household.

Columns: client_id, member_name, delivery_address, menu_type, food_notes,
allergies, kitchen, cadence, case_authorization_status, program_name.

Usage:
    python manage.py export_enrollments                     # both files, cwd
    python manage.py export_enrollments --out-dir /tmp      # both files, /tmp
    python manage.py export_enrollments --stage kitchen_assignment   # one file
"""
import csv
import os

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import (
    CaseType,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
)
from api.portal.serializers import internal_service_case
from api.services.delivery import current_household_cadence

# Stage value -> output filename stem.
_STAGE_FILES = {
    EnrollmentStage.KITCHEN_ASSIGNMENT: "kitchen_assignment",
    EnrollmentStage.PENDING_VERIFICATION: "pending_verification",
}
_ALLERGY_LABELS = dict(FoodAllergy.choices)
_CADENCE_LABELS = dict(DeliveryCadence.choices)

_COLUMNS = [
    "client_id",
    "member_name",
    "delivery_address",
    "menu_type",
    "food_notes",
    "allergies",
    "kitchen",
    "cadence",
    "case_authorization_status",
    "program_name",
]


def _format_address(addr):
    """One-line delivery address, or '' when none is set."""
    if addr is None:
        return ""
    line = ", ".join(
        p for p in (addr.street, addr.unit, addr.city) if (p or "").strip()
    )
    tail = " ".join(p for p in (addr.state, addr.zip) if (p or "").strip())
    return ", ".join(p for p in (line, tail) if p).strip()


def _allergies(codes):
    return "; ".join(_ALLERGY_LABELS.get(c, c) for c in (codes or []) if c and c != "none")


class Command(BaseCommand):
    help = (
        "Export enrollments at the Kitchen Assignment and/or Pending Verification "
        "stage to CSV, one row per household member."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--stage",
            choices=[EnrollmentStage.KITCHEN_ASSIGNMENT, EnrollmentStage.PENDING_VERIFICATION],
            help="Export only this stage (default: both, as two separate files).",
        )
        parser.add_argument(
            "--out-dir", default=".", help="Directory to write the CSV file(s) into."
        )

    def handle(self, *args, **options):
        stages = [options["stage"]] if options["stage"] else list(_STAGE_FILES)
        out_dir = options["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        stamp = timezone.localdate().isoformat()

        for stage in stages:
            path = os.path.join(out_dir, f"{_STAGE_FILES[stage]}_{stamp}.csv")
            n_rows, n_enr = self._export_stage(stage, path)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{EnrollmentStage(stage).label}: {n_enr} enrollments, "
                    f"{n_rows} member rows -> {path}"
                )
            )

    def _export_stage(self, stage, path):
        enrollments = (
            EnrollmentVerification.objects.filter(stage=stage)
            .select_related("client", "case", "case__program", "kitchen", "delivery_address")
            .prefetch_related("member_profiles__client", "delivery_schedules")
            .order_by("client__last_name", "client__first_name")
        )
        n_rows = n_enr = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
            writer.writeheader()
            for enr in enrollments:
                n_enr += 1
                address = _format_address(enr.delivery_address)
                kitchen = enr.kitchen.name if enr.kitchen_id else ""
                cadence_code = current_household_cadence(enr)
                cadence = _CADENCE_LABELS.get(cadence_code, cadence_code or "")
                auth, program = self._case_fields(enr)

                for m in enr.member_profiles.all():
                    writer.writerow({
                        "client_id": str(m.client.client_id) if m.client_id else "",
                        "member_name": m.member_name,
                        "delivery_address": address,
                        "menu_type": m.menu_type,
                        "food_notes": m.kitchen_food_notes or m.other_dietary_restrictions,
                        "allergies": _allergies(m.food_allergies),
                        "kitchen": kitchen,
                        "cadence": cadence,
                        "case_authorization_status": auth,
                        "program_name": program,
                    })
                    n_rows += 1
        return n_rows, n_enr

    def _case_fields(self, enr):
        """Authorization status + program name from the INTERNAL-SERVICE case.

        Prefer the enrollment's linked case when it is internal-service; else
        look one up on the client."""
        case = enr.case
        if case is None or case.case_type != CaseType.INTERNAL_SERVICE:
            case = internal_service_case(enr.client) or case
        auth = ""
        if case is not None:
            auth = (
                case.service_authorization_status_label
                or case.get_service_authorization_status_display()
            )
        program = enr.program_name
        if not program and case is not None:
            program = case.program_name or (case.program.name if case.program_id else "")
        return auth, program
