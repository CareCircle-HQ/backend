"""Repair clients whose VERIFIED menu/dietary was reset by a governing-case
replacement that reused a placeholder enrollment (see
``list_menu_carry_regressions`` for the read-only audit).

For each affected survivor enrollment (``supersedes`` a closed
``close_reason='case_replaced'`` source), restore ONLY the ``menu_type`` from the
previous (verified) enrollment -- KEEPING the member's current Dietary
Restrictions / Food Allergies / Other Restrictions untouched (they may have been
updated since; only the menu was wrongly reset). Then, when the survivor is
actively serving, re-run the kitchen meal-rule and rebuild the delivery calendar
(the menu drives fulfillment). Members the assigned kitchen can no longer fulfill
on the restored menu become Out of Orbit and are printed at the end for manual
review.

DRY-RUN by default (prints what WOULD change); pass ``--apply`` to commit. Use
``--client <id>`` to repair a single member. Idempotent: a member already matching
its verified source's menu is skipped.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import EnrollmentStage, EnrollmentVerification, MemberStatus


# The bug reset a placeholder survivor to the DEFAULT menu, dropping the verified
# source's SPECIAL menu -- so ONLY "special -> Standard/blank" is a regression.
# The reverse (Standard -> special) and special -> special are legitimate menu
# changes, NOT the bug, and must never be "restored" (that would break them).
_DEFAULT_MENUS = {"", "standard"}


def _is_default_menu(menu):
    return (menu or "").strip().lower() in _DEFAULT_MENUS


class Command(BaseCommand):
    help = (
        "Restore the verified menu onto survivors reset to the default menu by a "
        "case-replaced fork (dry-run; --apply to commit)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Commit the changes (default is a dry-run that changes nothing).",
        )
        parser.add_argument(
            "--client", default="",
            help="Only repair this client_id (default: every affected client).",
        )

    def handle(self, *args, **opts):
        from api.services.meal_rules import reconcile_member_kitchen_output
        from api.services.orders import (
            rebuild_delivery_calendar, recompute_delivery_plan,
        )

        apply = opts["apply"]
        only = (opts.get("client") or "").strip()

        survivors = (
            EnrollmentVerification.objects
            .filter(supersedes__isnull=False)
            .select_related("supersedes", "client", "kitchen")
            .prefetch_related("member_profiles", "supersedes__member_profiles")
        )
        if only:
            survivors = survivors.filter(client__client_id=only)

        header = f"{'client_id':<38}{'current_menu_type':<20}old_menu_type"
        self.stdout.write(header)

        members = set()       # distinct affected member client_ids (matches audit)
        enrollments = set()   # distinct survivor enrollments touched
        out_of_orbit = set()  # repaired members the kitchen can't fulfill (review)
        profiles_updated = 0
        calendars_rebuilt = 0
        for e_new in survivors.iterator(chunk_size=500):
            e_old = e_new.supersedes
            if not e_old or (e_old.close_reason or "") != "case_replaced":
                continue
            oldp = {p.client_id: p for p in e_old.member_profiles.all()}
            changed = []  # (profile, source_menu_type) for mismatched members
            for pn in e_new.member_profiles.all():
                po = oldp.get(pn.client_id)
                if po is None:
                    continue
                om = (po.menu_type or "").strip()
                nm = (pn.menu_type or "").strip()
                # Regression ONLY: the verified source had a SPECIAL menu and the
                # survivor was reset to the DEFAULT (Standard/blank). Skip the
                # reverse + special->special (legitimate, not the bug).
                if (not _is_default_menu(om)) and _is_default_menu(nm):
                    members.add(pn.client_id)
                    # Print the AFFECTED member (may be a dependent, not the
                    # enrollment owner).
                    self.stdout.write(
                        f"{str(pn.client_id):<38}{(nm or '(blank)'):<20}{om}"
                    )
                    changed.append((pn, po.menu_type))
            if not changed:
                continue
            enrollments.add(e_new.pk)

            if not apply:
                continue

            with transaction.atomic():
                # Restore ONLY the menu type from the previous (verified)
                # enrollment. KEEP the member's current Dietary Restrictions /
                # Food Allergies / Other Restrictions untouched (they may have been
                # updated since -- only the menu was wrongly reset).
                for pn, src_menu in changed:
                    pn.menu_type = src_menu
                    pn.save(update_fields=["menu_type"])
                    profiles_updated += 1
                # Only re-derive fulfillment for an actively-serving survivor --
                # never reactivate a closed / on-hold enrollment. The restored menu
                # may no longer be fulfillable by the assigned kitchen -> Out of
                # Orbit (printed at the end for manual review).
                if EnrollmentStage(e_new.stage) == EnrollmentStage.SERVICE_ACTIVE:
                    for mv in e_new.member_profiles.all():
                        try:
                            # allow_resume=False: NEVER touch a PAUSED / on-hold /
                            # INACTIVE / Out-of-Range member's status -- the meal
                            # rule may only toggle ACTIVE <-> OUT_OF_ORBIT here.
                            reconcile_member_kitchen_output(
                                mv, kitchen=e_new.kitchen, allow_resume=False,
                                save=True,
                            )
                        except Exception:  # pragma: no cover - defensive
                            pass
                    try:
                        recompute_delivery_plan(e_new)
                        rebuild_delivery_calendar(e_new)
                        calendars_rebuilt += 1
                    except Exception:  # pragma: no cover - defensive
                        self.stderr.write(
                            f"  calendar rebuild failed for enr {e_new.pk}"
                        )
                    for pn, _src in changed:
                        try:
                            pn.refresh_from_db()
                        except Exception:  # pragma: no cover - defensive
                            continue
                        if pn.status == MemberStatus.OUT_OF_ORBIT:
                            out_of_orbit.add(pn.client_id)

        if apply and out_of_orbit:
            self.stdout.write(
                "\nOut of Orbit after repair (assigned kitchen can't make the "
                "restored menu -- check manually):"
            )
            for cid in sorted(str(x) for x in out_of_orbit):
                self.stdout.write(f"  {cid}")

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {len(members)} affected members across "
            f"{len(enrollments)} enrollments"
            + (f" -- {profiles_updated} menus restored, "
               f"{calendars_rebuilt} calendars rebuilt, "
               f"{len(out_of_orbit)} now Out of Orbit." if apply
               else " would be repaired.")
        ))
