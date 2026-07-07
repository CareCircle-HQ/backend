"""One-off: mark a roster of members Verified (VER-Verified.xlsx), then let the
case authorization decide the resulting stage.

The rule (applied to EVERY client in the sheet):

  1. **Bring in / update the info from the sheet** -- menu type, food allergies,
     dietary restrictions, notes and the delivery address -- onto the member's
     MemberDietaryProfile (creating a household enrollment when none exists).
     The sheet is authoritative: existing dietary/product data is overwritten
     (it may differ from what we hold).

  2. **Mark Verified** -- stamp ``verified_at`` and move the enrollment to
     VERIFIED:
       * not-yet-verified (no enrollment / earlier stage) -> advance to VERIFIED;
       * already Verified -> left there;
       * AHEAD of Verified (Kitchen Assignment / Service Active) -> REGRESSED to
         VERIFIED. That isn't a legal transition, so the stage is set directly,
         a StageEvent is written, and any live delivery schedules are cancelled
         so previously-Active members drop off every Purchase Order.

  3. **Project the authorization** via ``reconcile_enrollment_authorization``:
     an APPROVED governing case advances Verified -> Kitchen Assignment;
     pending / denied / expired leave the client at Verified.

Dry-run unless --apply; --force is required to COMMIT when warnings exist
(members regressed OUT of Service Active -- their schedules are cancelled).

Usage:
    python manage.py mark_verified_from_file                 # dry run
    python manage.py mark_verified_from_file --apply --force  # commit
    python manage.py mark_verified_from_file --file other.xlsx
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.management.commands.activate_members_from_file import (
    _MENU_ALIAS,
    _MENU_TO_CATEGORY,
    parse_allergies,
    parse_restrictions,
)
from api.management.commands.hold_pending_closure_from_file import _get_client, _read_rows
from api.models import (
    Address,
    EnrollmentStage,
    EnrollmentVerification,
    MemberDietaryProfile,
    MemberStatus,
    MenuCategory,
    MenuType,
    OrderSchedule,
    OrderStatus,
    ScheduleStatus,
    StageEntityType,
    StageEvent,
    StageEventSource,
    MemberDeliverySchedule,
)
from api.serializers import ensure_household_with_primary
from api.services.catalog import menu_type_for_member
from api.services.lifecycle import (
    ENROLLMENT_TRANSITIONS,
    advance_enrollment,
    recompute_enrollment_household,
    reconcile_enrollment_authorization,
)

_DEFAULT_FILE = "tmp/verification/VER-Verified.xlsx"

# Stages ahead of Verified -- regressing one of these is destructive (pulls the
# household out of service), so it triggers a commit warning.
_AHEAD_OF_VERIFIED = {
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_ACTIVE,
    EnrollmentStage.SERVICE_COMPLETE,
}


class Command(BaseCommand):
    help = (
        "Mark a roster of members Verified (updating dietary/address info from "
        "the sheet), then let the case authorization advance approved ones to "
        "Kitchen Assignment. Dry-run unless --apply; --force past warnings."
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
            f"Verified roster: {path} -> {len(rows)} client rows"
        ))

        self.menus = {m.name.strip().lower(): m.name for m in MenuType.objects.all()}
        report = Counter()
        self.final_stage = Counter()
        self.missing = []
        self.regressed_from_service = []   # warnings
        self.blocked = False

        with transaction.atomic():
            for rec in rows:
                try:
                    with transaction.atomic():
                        bucket = self._process(rec, report)
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

    def _process(self, rec, report):
        client = _get_client(rec["id"])
        if client is None:
            self.missing.append(rec["id"])
            return "missing"

        household = ensure_household_with_primary(client)
        enr = household.enrollment_verifications.order_by("-opened_at").first()
        created = enr is None
        if created:
            primary_hm = household.members.filter(is_primary=True).select_related("client").first()
            enr = EnrollmentVerification.objects.create(
                client=primary_hm.client if primary_hm else client,
                household=household,
                stage=EnrollmentStage.PENDING_VERIFICATION,
            )

        # Delivery address + dietary info from the sheet (authoritative overwrite).
        self._update_address(enr, rec)
        self._upsert_profile(enr, client, rec)

        # Stamp the verification fact.
        if enr.verified_at is None:
            enr.verified_at = timezone.now()
            enr.save(update_fields=["verified_at"])

        # Move to VERIFIED.
        from_stage = EnrollmentStage(enr.stage)
        if from_stage == EnrollmentStage.VERIFIED:
            outcome = "already_verified"
        elif EnrollmentStage.VERIFIED in ENROLLMENT_TRANSITIONS.get(from_stage, set()):
            advance_enrollment(
                enr, EnrollmentStage.VERIFIED, force=True,
                note="Verified import: marked verified.",
            )
            outcome = "created" if created else "advanced"
        else:
            # Regress (e.g. Kitchen Assignment / Service Active -> Verified):
            # not a legal transition, so set directly + cancel live schedules.
            if from_stage in _AHEAD_OF_VERIFIED:
                self.regressed_from_service.append((str(client.client_id), from_stage))
            OrderSchedule.objects.filter(
                enrollment=enr, status=OrderStatus.SCHEDULED
            ).update(status=OrderStatus.CANCELLED)
            MemberDeliverySchedule.objects.filter(
                enrollment=enr, status=ScheduleStatus.SCHEDULED
            ).update(status=ScheduleStatus.CANCELLED)
            enr.stage = EnrollmentStage.VERIFIED
            enr.stage_at = timezone.now()
            enr.save(update_fields=["stage", "stage_at"])
            StageEvent.objects.create(
                entity_type=StageEntityType.ENROLLMENT,
                enrollment=enr, client=enr.client,
                from_stage=from_stage, to_stage=EnrollmentStage.VERIFIED,
                source=StageEventSource.MANUAL,
                note="Verified import: regressed to Verified (auth pending).",
            )
            recompute_enrollment_household(enr)
            outcome = "regressed"

        # Authorization projection: APPROVED -> Kitchen Assignment, else stays.
        reconcile_enrollment_authorization(
            enr, note="Verified import: authorization projection."
        )
        enr.refresh_from_db()
        self.final_stage[enr.stage] += 1
        return outcome

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

    def _upsert_profile(self, enr, client, rec):
        menu = self._resolve_menu(rec["meal"]) or menu_type_for_member(
            food_allergies=[], meal_category=rec["meal"]
        )
        allergies, unknown_al = parse_allergies(rec["allergy"])
        restrictions, unknown_re = parse_restrictions(rec["other_restr"])
        notes = " | ".join(
            x for x in ([rec["other_allergy"]] + unknown_al + unknown_re) if x
        )
        fields = dict(
            member_name=f"{client.first_name} {client.last_name}".strip(),
            menu_type=menu,
            food_allergies=allergies,
            dietary_restrictions=restrictions,
            other_dietary_restrictions=notes,
            meal_category=_MENU_TO_CATEGORY.get(menu.lower(), MenuCategory.FRESH_MEAL),
            general_verification_notes=rec["verif_note"],
        )
        profile = MemberDietaryProfile.objects.filter(
            enrollment=enr, client=client
        ).first()
        if profile is None:
            MemberDietaryProfile.objects.create(
                enrollment=enr, client=client, status=MemberStatus.ACTIVE, **fields
            )
        else:
            for k, v in fields.items():
                setattr(profile, k, v)
            profile.save()

    def _resolve_menu(self, meal_category):
        key = meal_category.strip().lower()
        key = _MENU_ALIAS.get(key, key)
        return self.menus.get(key)

    def _report(self, report, apply, force, total):
        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Summary ==="))
        self.stdout.write(self.style.SUCCESS(
            f"  Verified (new enrollment)          : {report.get('created', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Verified (advanced from earlier)   : {report.get('advanced', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Regressed to Verified              : {report.get('regressed', 0)}"
        ))
        self.stdout.write(
            f"  Already Verified                   : {report.get('already_verified', 0)}"
        )
        self.stdout.write(self.style.WARNING(
            f"  Missing / errored                  : {report.get('missing', 0) + report.get('error', 0)}"
        ))
        self.stdout.write(f"  {'TOTAL rows':<34}: {total}")

        self.stdout.write(head("\nFinal enrollment stage:"))
        for stage, n in self.final_stage.most_common():
            self.stdout.write(f"  {stage}: {n}")

        if self.regressed_from_service:
            self.stdout.write(self.style.ERROR(
                f"\n!!! WARNING: {len(self.regressed_from_service)} member(s) "
                "regressed OUT of service (Kitchen Assignment / Service Active). "
                "Active members' schedules were cancelled (off POs). !!!"
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
