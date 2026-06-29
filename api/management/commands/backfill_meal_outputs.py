"""Backfill the kitchen meal type / food note (the PO values) on member dietary
profiles that have a menu type + a kitchen assigned but are missing those
outputs -- a gap left by earlier imports.

It applies the EXACT same ruleset used at kitchen assignment
(``api.services.meal_rules.apply_to_member`` -> ``resolve_kitchen_meal``), so
the outputs match what the Logistics "Assign Kitchen" action would produce:
  * a fulfillable member gets ``kitchen_meal_type`` / ``kitchen_food_notes``;
  * an unfulfillable member is flagged OUT_OF_ORBIT (and dropped from POs), with
    the same timeline event the kitchen-assignment flow emits.

Target (default): profiles with a menu type, an assigned kitchen, status ACTIVE,
and a blank ``kitchen_meal_type``. Use ``--include-no-kitchen`` to also process
profiles whose enrollment has no kitchen yet.

Dry-run unless ``--apply`` so you can review (including who would go Out of
Orbit) before committing.

Usage:
    python manage.py backfill_meal_outputs
    python manage.py backfill_meal_outputs --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import MemberDietaryProfile, MemberStatus
from api.services import timeline
from api.services.meal_rules import apply_to_member


class Command(BaseCommand):
    help = (
        "Recompute kitchen meal type / food note (PO values) for member profiles "
        "missing them, using the same ruleset as kitchen assignment. Dry-run "
        "unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument("--limit", type=int, default=0, help="First N profiles.")
        parser.add_argument(
            "--include-no-kitchen",
            action="store_true",
            help="Also process profiles whose enrollment has no kitchen assigned.",
        )

    def _queryset(self, include_no_kitchen):
        # Active members with a menu type but no PO meal type yet. Out-of-orbit
        # members intentionally have a blank kitchen_meal_type, so ACTIVE-only.
        qs = (
            MemberDietaryProfile.objects.select_related("enrollment", "client")
            .filter(status=MemberStatus.ACTIVE, kitchen_meal_type="")
            .exclude(menu_type="")
        )
        if not include_no_kitchen:
            qs = qs.filter(enrollment__kitchen__isnull=False)
        return qs.order_by("enrollment_id")

    def handle(self, *args, **options):
        apply = options["apply"]
        qs = self._queryset(options["include_no_kitchen"])
        if options["limit"]:
            qs = qs[: options["limit"]]

        report = Counter()
        filled_samples = []
        out_of_orbit = []

        with transaction.atomic():
            for profile in qs:
                result, became_out = apply_to_member(profile, save=apply)
                if result.out_of_orbit:
                    report["flagged_out_of_orbit"] += 1
                    out_of_orbit.append(
                        (profile.member_name or str(profile.client_id), profile.menu_type)
                    )
                    if apply and became_out:
                        try:
                            timeline.event_for_out_of_orbit(
                                profile, enrollment=profile.enrollment,
                                reason="Allergy/menu combination cannot be safely fulfilled.",
                                actor="system:backfill_meal_outputs",
                            )
                        except Exception:  # never let history-logging break the fix
                            pass
                else:
                    report["filled"] += 1
                    if len(filled_samples) < 15:
                        filled_samples.append(
                            (profile.member_name or str(profile.client_id),
                             profile.kitchen_meal_type, profile.kitchen_food_notes)
                        )

            if not apply:
                transaction.set_rollback(True)

        self._report(report, filled_samples, out_of_orbit, apply)

    def _report(self, report, filled_samples, out_of_orbit, apply):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Backfill kitchen meal outputs ==="))
        self.stdout.write(f"  {'Filled (kitchen meal type set)':<40}: {report.get('filled', 0)}")
        self.stdout.write(f"  {'Flagged Out of Orbit':<40}: {report.get('flagged_out_of_orbit', 0)}")
        self.stdout.write(f"  {'TOTAL reviewed':<40}: {sum(report.values())}")

        if filled_samples:
            self.stdout.write(head("\nSample fills (name | meal type | food note):"))
            for name, meal, note in filled_samples:
                self.stdout.write(f"  {name[:28]:28} | {meal!r} | {note!r}")

        if out_of_orbit:
            self.stdout.write(head(f"\nWould flag Out of Orbit ({len(out_of_orbit)}, up to 30):"))
            for name, menu in out_of_orbit[:30]:
                self.stdout.write(f"  {name[:28]:28} | menu={menu!r}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN: rolled back. Re-run with --apply to commit.")
            )
