"""Check whether we have a kitchen that can serve each individual member who is
either awaiting kitchen assignment or already active, based on the member's
dietary needs (menu type + food allergies) vs. every kitchen's offered menus
and their restrictions.

Members in scope: ``MemberDietaryProfile`` rows with status ACTIVE whose
enrollment is at the KITCHEN_ASSIGNMENT (waiting) or SERVICE_ACTIVE (active)
stage. For each, we list the kitchens that can serve them. A member with NO
serving kitchen is a candidate to be put Out of Orbit.

The command PRINTS A PLAN of what it will do and asks for confirmation before it
runs (skip with --yes). It always writes a CSV report. With --apply it also sets
every uncoverable member to Out of Orbit -- emitting a timeline event with the
reason and adding an agent Note on the member's client.

Usage:
    python manage.py report_kitchen_coverage
    python manage.py report_kitchen_coverage --out ~/kitchen_coverage.csv
    python manage.py report_kitchen_coverage --apply --yes
"""
import csv
import sys
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from api.history import ChangeSource
from api.models import (
    EnrollmentStage,
    Kitchen,
    KitchenStatus,
    MemberDietaryProfile,
    MemberStatus,
    Note,
    NoteSource,
)
from api.services import timeline
from api.services.kitchens import (
    required_product_for_program,
    serving_kitchens_for_member,
)

_DEFAULT_STAGES = [EnrollmentStage.KITCHEN_ASSIGNMENT, EnrollmentStage.SERVICE_ACTIVE]
_DEFAULT_REASON = "No available kitchen can serve this member's menu type / allergies."
_ACTOR = "system:kitchen-coverage"


class Command(BaseCommand):
    help = (
        "Report members (awaiting kitchen assignment or active) that no kitchen "
        "can serve, and optionally put them Out of Orbit. Writes a CSV."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default="",
            help="CSV output path (default: kitchen_coverage_<timestamp>.csv in cwd).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Set members with no serving kitchen to Out of Orbit.",
        )
        parser.add_argument(
            "--reason",
            default=_DEFAULT_REASON,
            help="Reason recorded on the Out of Orbit event + client Note (with --apply).",
        )
        parser.add_argument(
            "--ignore-product",
            action="store_true",
            help="Ignore the program's product kind (meals/boxes) when matching kitchens.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt.",
        )

    def handle(self, *args, **options):
        out_path = options["out"] or f"kitchen_coverage_{datetime.now():%Y%m%d_%H%M%S}.csv"
        apply = options["apply"]
        reason = (options["reason"] or _DEFAULT_REASON).strip()
        ignore_product = options["ignore_product"]

        profiles = list(
            MemberDietaryProfile.objects.filter(
                status=MemberStatus.ACTIVE,
                enrollment__stage__in=_DEFAULT_STAGES,
            )
            .select_related("client", "enrollment", "enrollment__case", "enrollment__case__program")
            .order_by("enrollment__code", "member_name")
        )
        kitchens = list(
            Kitchen.objects.all()
            .prefetch_related("kitchen_menu_types__menu_type", "kitchen_menu_types__restrictions")
            .order_by("name")
        )
        active_kitchens = [k for k in kitchens if k.status == KitchenStatus.ACTIVE]

        # --- Plan: tell the user what will happen, then confirm. ---
        if not self._confirm_plan(profiles, active_kitchens, out_path, apply, ignore_product, options["yes"]):
            self.stdout.write(self.style.WARNING("Aborted."))
            return

        report = Counter()
        rows = []
        no_kitchen = []  # profiles with zero serving kitchens

        for p in profiles:
            program = self._program_name(p)
            required_product = None if ignore_product else required_product_for_program(program)
            serving = serving_kitchens_for_member(
                p, kitchens=kitchens, required_product=required_product, active_only=True
            )
            served = bool(serving)
            report["served" if served else "no_kitchen"] += 1
            if not served:
                no_kitchen.append(p)
            rows.append(self._row(p, program, serving, served))

        # --- Apply (optional): put uncoverable members Out of Orbit. ---
        applied = 0
        if apply and no_kitchen:
            with transaction.atomic():
                for p in no_kitchen:
                    if self._set_out_of_orbit(p, reason):
                        applied += 1
            # Reflect the applied action in the CSV verdict column.
            applied_ids = {p.pk for p in no_kitchen}
            for r in rows:
                if r["member_id"] in applied_ids:
                    r["action"] = "set_out_of_orbit"

        self._write_csv(out_path, rows)
        self._report(report, out_path, apply, applied, len(no_kitchen))

    # ------------------------------------------------------------------
    def _program_name(self, profile):
        enr = profile.enrollment
        if enr.case_id and enr.case and enr.case.program_id and enr.case.program:
            return enr.case.program.name
        return enr.program_name or ""

    def _row(self, profile, program, serving, served):
        return {
            "member_id": profile.pk,
            "client_id": str(profile.client_id) if profile.client_id else "",
            "member_name": profile.member_name or "",
            "enrollment_code": profile.enrollment.code or "",
            "stage": profile.enrollment.get_stage_display(),
            "program": program,
            "menu_type": profile.menu_type or "",
            "food_allergies": ", ".join(profile.food_allergies or []),
            "dietary_restrictions": ", ".join(profile.dietary_restrictions or []),
            "serving_kitchen_count": len(serving),
            "serving_kitchens": "; ".join(s["kitchen"].name for s in serving),
            "verdict": "served" if served else "NO_KITCHEN",
            "action": "none",
        }

    def _set_out_of_orbit(self, profile, reason):
        """Force a member Out of Orbit: clear kitchen outputs, emit the timeline
        event, add an agent Note. Isolated so one failure doesn't abort the run."""
        try:
            with transaction.atomic():
                profile.status = MemberStatus.OUT_OF_ORBIT
                profile.kitchen_meal_type = ""
                profile.kitchen_food_notes = ""
                profile.save(update_fields=[
                    "status", "kitchen_meal_type", "kitchen_food_notes", "updated_at",
                ])
                try:
                    timeline.event_for_out_of_orbit(
                        profile, enrollment=profile.enrollment, reason=reason,
                        source=ChangeSource.SYSTEM, actor=_ACTOR,
                    )
                except Exception:
                    pass
                if profile.client_id:
                    Note.objects.create(
                        client=profile.client,
                        source=NoteSource.AGENT,
                        author_name="kitchen-coverage",
                        body=f"Set Out of Orbit. Reason: {reason}",
                    )
            return True
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"  Failed to set {profile.member_name or profile.pk} Out of Orbit: {exc}"
            ))
            return False

    # ------------------------------------------------------------------
    def _confirm_plan(self, profiles, active_kitchens, out_path, apply, ignore_product, yes):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Kitchen coverage check — plan ==="))
        self.stdout.write(
            "  Scope: ACTIVE members awaiting kitchen assignment or in active service."
        )
        self.stdout.write(f"  Members to evaluate            : {len(profiles)}")
        self.stdout.write(f"  Active kitchens considered     : {len(active_kitchens)}")
        self.stdout.write(
            f"  Product-kind matching          : {'ignored' if ignore_product else 'enforced'}"
        )
        self.stdout.write(f"  Will WRITE CSV report to       : {out_path}")
        if apply:
            self.stdout.write(self.style.WARNING(
                "  Will SET members with no serving kitchen to Out of Orbit\n"
                "    (emits a timeline event + adds an agent Note per member)."
            ))
        else:
            self.stdout.write(
                "  Read-only: NO member statuses will change (use --apply to act)."
            )

        if yes:
            return True
        if not sys.stdin or not sys.stdin.isatty():
            # Non-interactive: don't block automation; report-only is safe, but
            # never auto-apply destructive changes without an explicit --yes.
            if apply:
                self.stdout.write(self.style.ERROR(
                    "\nRefusing to --apply non-interactively without --yes."
                ))
                return False
            return True
        answer = input("\nProceed? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    def _write_csv(self, path, rows):
        fields = [
            "member_id", "client_id", "member_name", "enrollment_code", "stage",
            "program", "menu_type", "food_allergies", "dietary_restrictions",
            "serving_kitchen_count", "serving_kitchens", "verdict", "action",
        ]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

    def _report(self, report, out_path, apply, applied, no_kitchen_count):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Kitchen coverage — results ==="))
        self.stdout.write(f"  {'Served (>=1 kitchen)':<32}: {report.get('served', 0)}")
        self.stdout.write(f"  {'No kitchen can serve':<32}: {report.get('no_kitchen', 0)}")
        self.stdout.write(f"  {'TOTAL evaluated':<32}: {sum(report.values())}")
        self.stdout.write(self.style.SUCCESS(f"\nCSV written: {out_path}"))
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"APPLIED: {applied}/{no_kitchen_count} members set Out of Orbit."
            ))
        elif no_kitchen_count:
            self.stdout.write(self.style.WARNING(
                f"{no_kitchen_count} member(s) have no serving kitchen. "
                "Re-run with --apply to put them Out of Orbit."
            ))
