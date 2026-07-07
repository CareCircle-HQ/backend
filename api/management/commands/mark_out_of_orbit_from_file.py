"""One-off: mark a roster of members Out of Orbit (OOO-OutofOrbit.xlsx).

Per client this:

  1. **Brings in the required info** -- menu type, food allergies, dietary
     restrictions, other-restriction notes and the delivery address -- onto the
     member's MemberDietaryProfile (creating a household enrollment to hold it
     when none exists; reusing the existing one otherwise).

  2. **Marks the member Out of Orbit** (``MemberStatus.OUT_OF_ORBIT``) and clears
     the kitchen meal result, so the member is excluded from every delivery
     schedule / Purchase Order (the PO queries already exclude OOO members -- no
     schedule cancellation needed). This is a per-MEMBER status, not a household
     stage change, so the enrollment stage and the rest of the household are left
     untouched.

  3. **Adds a System note** using the sheet's reason (column G), e.g.
     "Set as Out of Orbit by the system import. Reason: Out of Orbit - Food",
     and emits the matching Out-of-Orbit timeline event.

Idempotent: re-running won't duplicate the note (deduped on content) or the
timeline event (deduped per enrollment+member), and the profile is upserted.

Dry-run unless --apply.

Usage:
    python manage.py mark_out_of_orbit_from_file                 # dry run
    python manage.py mark_out_of_orbit_from_file --apply          # commit
    python manage.py mark_out_of_orbit_from_file --file other.xlsx
"""
import hashlib
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

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
    Note,
    NoteSource,
)
from api.serializers import ensure_household_with_primary
from api.services import timeline
from api.services.catalog import menu_type_for_member

_DEFAULT_FILE = "tmp/verification/OOO-OutofOrbit.xlsx"


class Command(BaseCommand):
    help = (
        "Mark a roster of members Out of Orbit (excluded from POs), bringing in "
        "their dietary + address info and adding a System note from the sheet's "
        "reason column. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE)
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]

        rows = _read_rows(path)
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Out of Orbit roster: {path} -> {len(rows)} client rows"
        ))

        self.menus = {m.name.strip().lower(): m.name for m in MenuType.objects.all()}
        report = Counter()
        self.missing = []
        self.was_active = []   # previously Active/KA members now OOO (reported)

        with transaction.atomic():
            for rec in rows:
                try:
                    with transaction.atomic():
                        bucket = self._process(rec)
                except Exception as exc:
                    bucket = "error"
                    self.missing.append(f"{rec['id']} (error: {exc})")
                report[bucket] += 1

            if not apply:
                transaction.set_rollback(True)

        self._report(report, apply, len(rows))

    def _process(self, rec):
        client = _get_client(rec["id"])
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

        # Delivery address from the sheet.
        if any(rec[k] for k in ("street", "city", "state", "zip")):
            address = Address.objects.create(
                client=enr.client, type="temporary",
                street=rec["street"], unit=rec["apt"], city=rec["city"],
                state=rec["state"], zip=rec["zip"], notes=rec["addr_notes"],
            )
            enr.delivery_address = address
            enr.save(update_fields=["delivery_address"])

        # Dietary info from the sheet.
        menu = self._resolve_menu(rec["meal"]) or menu_type_for_member(
            food_allergies=[], meal_category=rec["meal"]
        )
        allergies, unknown_al = parse_allergies(rec["allergy"])
        restrictions, unknown_re = parse_restrictions(rec["other_restr"])
        notes = " | ".join(
            x for x in ([rec["other_allergy"]] + unknown_al + unknown_re) if x
        )

        profile = MemberDietaryProfile.objects.filter(
            enrollment=enr, client=client
        ).first()
        prev_status = profile.status if profile else None
        fields = dict(
            member_name=f"{client.first_name} {client.last_name}".strip(),
            menu_type=menu,
            food_allergies=allergies,
            dietary_restrictions=restrictions,
            other_dietary_restrictions=notes,
            meal_category=_MENU_TO_CATEGORY.get(menu.lower(), MenuCategory.FRESH_MEAL),
            general_verification_notes=rec["verif_note"],
            # Out of Orbit: excluded from POs; clear the kitchen meal result.
            status=MemberStatus.OUT_OF_ORBIT,
            kitchen_meal_type="",
            kitchen_food_notes="",
        )
        if profile is None:
            profile = MemberDietaryProfile.objects.create(
                enrollment=enr, client=client, **fields
            )
        else:
            for k, v in fields.items():
                setattr(profile, k, v)
            profile.save()

        if prev_status in (MemberStatus.ACTIVE, None) and enr.stage in (
            EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.KITCHEN_ASSIGNMENT
        ):
            self.was_active.append(str(client.client_id))

        # System note using the sheet reason (column G), deduped on content.
        reason = rec["reason"]
        body = "Set as Out of Orbit by the system import."
        if reason:
            body += f" Reason: {reason}"
        chash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        note_created = False
        if not Note.objects.filter(
            client=client, source=NoteSource.SYSTEM, content_hash=chash
        ).exists():
            Note.objects.create(
                client=client, source=NoteSource.SYSTEM, body=body, content_hash=chash,
            )
            note_created = True

        # Out-of-Orbit timeline event (deduped per enrollment+member).
        try:
            timeline.event_for_out_of_orbit(
                profile, enrollment=enr, reason=reason, actor="System import",
            )
        except Exception:  # never let history-logging break the import
            pass

        return "marked_new_note" if note_created else "marked_note_exists"

    def _resolve_menu(self, meal_category):
        key = meal_category.strip().lower()
        key = _MENU_ALIAS.get(key, key)
        return self.menus.get(key)

    def _report(self, report, apply, total):
        head = self.style.MIGRATE_HEADING
        marked = report.get("marked_new_note", 0) + report.get("marked_note_exists", 0)
        self.stdout.write(head("\n=== Summary ==="))
        self.stdout.write(self.style.SUCCESS(
            f"  Members marked Out of Orbit        : {marked}"
        ))
        self.stdout.write(
            f"    - with a new note                : {report.get('marked_new_note', 0)}"
        )
        self.stdout.write(
            f"    - note already existed           : {report.get('marked_note_exists', 0)}"
        )
        self.stdout.write(
            f"  Previously Active/Kitchen Assign.  : {len(self.was_active)}"
        )
        self.stdout.write(self.style.WARNING(
            f"  Missing / errored                  : {report.get('missing', 0) + report.get('error', 0)}"
        ))
        self.stdout.write(f"  {'TOTAL rows':<34}: {total}")

        if self.was_active:
            self.stdout.write(head(
                f"\nWere Active / Kitchen Assignment, now Out of Orbit "
                f"({len(self.was_active)}, up to 40):"
            ))
            for cid in self.was_active[:40]:
                self.stdout.write(f"  {cid}")

        if self.missing:
            self.stdout.write(head(f"\nMissing / errored ({len(self.missing)}, up to 60):"))
            for cid in self.missing[:60]:
                self.stdout.write(f"  {cid}")

        if apply:
            self.stdout.write(self.style.SUCCESS("\nAPPLIED (committed)."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))
