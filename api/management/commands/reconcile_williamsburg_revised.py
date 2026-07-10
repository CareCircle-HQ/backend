"""One-off: reconcile the DB against the REVISED Williamsburg client list.

Unlike ``build_williamsburg_households`` (which reads a household roster with
``HM #N`` member columns), the revised sheet is a FLAT per-client list:

    | Unite Us Client ID | Cadence | Facility |

Each row is one client (its own household primary). This command audits every
row against the DB and, with ``--apply``, brings it into line with the
Williamsburg exception (Kosher menu, Williamsburg kitchen, Service Active,
delivery address defaulted to the primary's current address).

For each row it classifies the client into exactly one bucket:

  * ``missing``       - client id not in the DB (can't be created from an id;
                        needs a manual import). Reported only.
  * ``dependent``     - client is a NON-primary member of someone else's
                        household. Skipped (never restructure) + flagged.
  * ``backfilled``    - found, not yet enrolled -> mark Williamsburg, ensure the
                        household, create a verification enrollment and run the
                        shared fast-track (Kosher / Williamsburg kitchen / the
                        row's cadence / Service Active, address defaulted).
  * ``corrected``     - found + enrolled but something was off and safely fixed
                        (missing ``is_williamsburg`` flag, or missing delivery
                        address defaulted to the primary's current address).
  * ``ok``            - found + enrolled + already fully correct.
  * ``review``        - found + enrolled but with a discrepancy this command
                        will NOT auto-fix (On Hold, non-Williamsburg kitchen,
                        non-Kosher profile, cadence mismatch, out-of-orbit
                        member). Reported for manual handling -- rebuilding an
                        active client's kitchen/cadence would destroy live
                        delivery schedules, so that stays manual.

Idempotent. Dry-run unless ``--apply``.

Usage:
    python manage.py reconcile_williamsburg_revised                 # dry run
    python manage.py reconcile_williamsburg_revised --apply          # commit
    python manage.py reconcile_williamsburg_revised --file other.xlsx
"""
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    Client,
    DeliveryCadence,
    EnrollmentStage,
    EnrollmentVerification,
    HouseholdMember,
    Kitchen,
    MemberStatus,
)
from api.portal.serializers import internal_service_case
from api.serializers import ensure_household_with_primary
from api.services.williamsburg import (
    WILLIAMSBURG_KITCHEN_NAME,
    WILLIAMSBURG_MENU_TYPE,
    _primary_current_address,
    fast_track_williamsburg_enrollment,
)

_DEFAULT_FILE = "tmp/verification/WilliamsburgClientsRevised.xlsx"
_PRIMARY_COL = "Unite Us Client ID"
_CADENCE_COL = "Cadence"
_FACILITY_COL = "Facility"

# Cadence code (revised sheet) -> (DeliveryCadence, delivery weekday codes).
# Only "A" is present today (Mon/Thu, matching the original Williamsburg flow).
# Unknown codes are flagged rather than silently activated with a wrong cadence.
_CADENCE_MAP = {
    "A": (DeliveryCadence.MON_THU, ["mon", "thu"]),
}


def _norm(value):
    return "" if value is None else str(value).strip()


def _read_rows(path):
    """Yield (client_id_lower, cadence_code, facility) per data row."""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    header = [_norm(c) for c in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    ci = idx.get(_PRIMARY_COL)
    cad_i = idx.get(_CADENCE_COL)
    fac_i = idx.get(_FACILITY_COL)
    out = []
    for r in rows[1:]:
        cid = _norm(r[ci]).lower() if ci is not None and ci < len(r) else ""
        if not cid:
            continue
        cad = _norm(r[cad_i]) if cad_i is not None and cad_i < len(r) else ""
        fac = _norm(r[fac_i]) if fac_i is not None and fac_i < len(r) else ""
        out.append((cid, cad, fac))
    return out


class Command(BaseCommand):
    help = (
        "Reconcile the DB against the revised (flat) Williamsburg client list: "
        "audit every row, backfill the not-yet-enrolled, and apply safe "
        "corrections. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=_DEFAULT_FILE, help="Revised .xlsx path.")
        parser.add_argument("--apply", action="store_true", help="Commit changes.")
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Required alongside --apply to COMMIT when warnings exist "
                "(clients On Hold / Out of Orbit / Paused). Acknowledges you have "
                "reviewed them."
            ),
        )
        parser.add_argument(
            "--assign-missing-kitchen", action="store_true",
            help=(
                "Also fast-track ENROLLED clients that have NO kitchen and NO "
                "existing schedule and are cleanly active: assign the Williamsburg "
                "kitchen, set Mon/Thu, rebuild every member to Kosher, build the "
                "delivery schedule + calendar, and activate to Service Active. "
                "On Hold / Out-of-Orbit / Paused are left for manual review."
            ),
        )

    def handle(self, *args, **options):
        path = options["file"]
        apply = options["apply"]
        force = options["force"]
        self.assign_missing_kitchen = options["assign_missing_kitchen"]
        # Clients found NOT cleanly active, surfaced as a prominent warning so a
        # prod operator sees them BEFORE committing.
        self.warn = {"on_hold": [], "out_of_orbit": [], "paused": []}
        self.blocked = False

        rows = _read_rows(path)
        if not rows:
            self.stdout.write(self.style.ERROR(f"No rows read from {path}."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Revised roster: {path} -> {len(rows)} client rows"
        ))

        wburg = Kitchen.objects.filter(name__iexact=WILLIAMSBURG_KITCHEN_NAME).first()
        if wburg is None:
            self.stdout.write(self.style.ERROR(
                f"Williamsburg kitchen ('{WILLIAMSBURG_KITCHEN_NAME}') not found."
            ))
            return

        report = Counter()
        flags = []  # (client_id, bucket, note)

        with transaction.atomic():
            for cid, cad, fac in rows:
                try:
                    with transaction.atomic():
                        bucket, note = self._process(cid, cad, fac, wburg)
                except Exception as exc:  # isolate a bad row
                    bucket, note = ("error", str(exc))
                report[bucket] += 1
                if bucket in ("missing", "dependent", "review", "error", "assigned"):
                    flags.append((cid, bucket, note))

            has_warnings = any(self.warn.values())
            if not apply:
                transaction.set_rollback(True)
            elif has_warnings and not force:
                # Prod safety: never commit while warnings exist unless the
                # operator explicitly acknowledges them with --force.
                transaction.set_rollback(True)
                self.blocked = True

        self._report(report, flags, apply, force, len(rows))

    # -- per-row -----------------------------------------------------------
    def _process(self, cid, cad, fac, wburg):
        client = Client.objects.filter(client_id=cid).first()
        if client is None:
            return ("missing", "client id not in DB")

        # Guard: never restructure a client who is a dependent elsewhere.
        membership = HouseholdMember.objects.filter(client=client).first()
        if membership is not None and not membership.is_primary:
            return ("dependent", "non-primary member of another household")

        # Williamsburg's default cadence is Mon/Thu (code "A"); a sheet without a
        # Cadence column (or a blank cell) falls back to it rather than flagging.
        cadence = _CADENCE_MAP.get((cad or "A").upper())
        if cadence is None:
            return ("review", f"unsupported cadence code {cad!r}")
        _cadence_enum, desired_weekdays = cadence

        enr = client.enrollments.order_by("-opened_at").first()
        if enr is None:
            return self._backfill(client)
        return self._correct(client, enr, wburg, desired_weekdays)

    def _collect_warnings(self, client, enr):
        """Record clients that are NOT cleanly active (On Hold / Out of Orbit /
        Paused) so the report can warn the operator before any commit."""
        cid = str(client.client_id)
        if enr.stage == EnrollmentStage.ON_HOLD:
            self.warn["on_hold"].append(cid)
        for mp in enr.member_profiles.all():
            if mp.status == MemberStatus.OUT_OF_ORBIT:
                self.warn["out_of_orbit"].append(f"{cid} ({mp.member_name})")
            elif mp.status == MemberStatus.PAUSED:
                self.warn["paused"].append(f"{cid} ({mp.member_name})")

    def _backfill(self, client):
        # Mark Williamsburg (operational flag + canonical lead_source), then run
        # the shared fast-track: Kosher / Williamsburg kitchen / Mon-Thu /
        # Service Active, delivery address defaulted to the primary's current.
        self._flag_williamsburg(client)
        household = ensure_household_with_primary(client)
        case = internal_service_case(client)
        program = case.program if (case and case.program_id) else None
        enr = EnrollmentVerification.objects.create(
            client=client,
            household=household,
            case=case,
            program_name=(program.name if program else "")
            or (case.program_name if case else ""),
            service_type=(case.service_type if case else "") or "",
            household_size=household.members.count(),
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        fast_track_williamsburg_enrollment(enr, actor=None, agent=None)
        return ("backfilled", "")

    def _correct(self, client, enr, wburg, desired_weekdays):
        fixed = []
        review = []

        self._collect_warnings(client, enr)

        if self._flag_williamsburg(client):
            fixed.append("set is_williamsburg")

        on_hold = enr.stage == EnrollmentStage.ON_HOLD
        has_ooo = enr.member_profiles.filter(status=MemberStatus.OUT_OF_ORBIT).exists()
        has_paused = enr.member_profiles.filter(status=MemberStatus.PAUSED).exists()

        # Assign a MISSING kitchen (--assign-missing-kitchen): an enrolled client
        # with no kitchen and no existing schedule that is cleanly active runs the
        # same fast-track as a backfill -- assign the Williamsburg kitchen, set
        # Mon/Thu, rebuild every member to Kosher, build the delivery schedule AND
        # dated calendar, and activate to Service Active. There are no live
        # schedules to destroy, so this is non-destructive. On Hold / Out-of-Orbit
        # / Paused are excluded (left for manual review below).
        if (
            self.assign_missing_kitchen
            and enr.kitchen_id is None
            and not enr.delivery_schedules.exists()
            and not on_hold and not has_ooo and not has_paused
        ):
            fast_track_williamsburg_enrollment(enr, actor=None, agent=None)
            enr.refresh_from_db()
            n_sched = enr.delivery_schedules.count()
            n_orders = enr.orders.count()
            note = (
                f"assigned Williamsburg kitchen + Mon/Thu, built {n_sched} schedule(s) "
                f"+ {n_orders} calendar order(s), activated"
            )
            if n_orders == 0:
                # Activated but the delivery calendar is EMPTY -- almost always a
                # missing case authorization window. Surface it: the client is NOT
                # truly ready to serve until the case has an approval window.
                return ("assigned", note + " -- WARNING: 0 calendar orders "
                        "(check case authorization window)")
            return ("assigned", note)

        # Missing delivery address -> default to the primary's current address.
        if enr.delivery_address_id is None:
            addr = _primary_current_address(client)
            if addr is not None:
                enr.delivery_address = addr
                enr.save(update_fields=["delivery_address"])
                fixed.append("defaulted delivery address")
            else:
                review.append("no delivery address and no primary address to use")

        # Discrepancies we DON'T auto-fix (would destroy live schedules): kitchen,
        # cadence, non-Kosher menu, out-of-orbit member, On Hold.
        if on_hold:
            review.append("enrollment On Hold")
        if enr.kitchen_id is None:
            review.append("no kitchen assigned")
        elif enr.kitchen_id != wburg.pk:
            review.append("kitchen is not Williamsburg")
        # Case-insensitive: the DB may store weekdays capitalized ("Mon"/"Thu").
        current_weekdays = [str(d).lower() for d in (enr.delivery_weekdays or [])]
        if tuple(current_weekdays) != tuple(desired_weekdays):
            review.append(
                f"cadence {enr.delivery_weekdays or []} != desired {desired_weekdays}"
            )
        if enr.member_profiles.exclude(menu_type__iexact=WILLIAMSBURG_MENU_TYPE).exists():
            review.append("has non-Kosher member profile")
        if has_ooo:
            review.append("has out-of-orbit member")
        if has_paused:
            review.append("has paused member")

        if review:
            return ("review", "; ".join(review) + (
                f" (auto-fixed: {', '.join(fixed)})" if fixed else ""
            ))
        if fixed:
            return ("corrected", "; ".join(fixed))
        return ("ok", "")

    @staticmethod
    def _flag_williamsburg(client):
        """Set is_williamsburg + canonical lead_source; return True if changed."""
        updates = []
        if not client.is_williamsburg:
            client.is_williamsburg = True
            updates.append("is_williamsburg")
        if (client.lead_source or "").strip().lower() != "williamsburg":
            client.lead_source = "Williamsburg"
            updates.append("lead_source")
        if updates:
            client.save(update_fields=updates)
        return bool(updates)

    # -- report ------------------------------------------------------------
    def _report(self, report, flags, apply, force, total):
        head = self.style.MIGRATE_HEADING

        # Prominent, up-front warning: clients that are NOT cleanly active. A
        # prod operator must see these before committing.
        total_warn = sum(len(v) for v in self.warn.values())
        if total_warn:
            self.stdout.write(self.style.ERROR(
                f"\n!!! WARNING: {total_warn} enrolled client(s) are NOT cleanly "
                "active !!!"
            ))
            labels = [
                ("on_hold", "On Hold (enrollment paused)"),
                ("out_of_orbit", "Out of Orbit member"),
                ("paused", "Paused member"),
            ]
            for key, label in labels:
                items = self.warn[key]
                if items:
                    self.stdout.write(self.style.WARNING(f"  {label} ({len(items)}):"))
                    for x in items:
                        self.stdout.write(f"      {x}")
            self.stdout.write(self.style.WARNING(
                "  NOTE: these are left untouched by this command (backfill only "
                "activates NOT-yet-enrolled clients). Resume/unpause them "
                "manually if they should be active."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nNo warnings: every enrolled client in the list is cleanly "
                "active (no On Hold / Out of Orbit / Paused)."
            ))

        self.stdout.write(head("\n=== Reconcile Williamsburg (revised) ==="))
        order = [
            ("backfilled", "Backfilled (activated Service Active)"),
            ("assigned", "Assigned kitchen + built schedule/calendar (activated)"),
            ("corrected", "Corrected (safe fixes applied)"),
            ("ok", "OK (already correct)"),
            ("review", "Needs manual review (see flags)"),
            ("dependent", "Skipped: dependent in another household"),
            ("missing", "Missing: client id not in DB"),
            ("error", "Errored (row rolled back)"),
        ]
        for key, label in order:
            if report.get(key):
                self.stdout.write(f"  {label:<46}: {report[key]}")
        self.stdout.write(f"  {'TOTAL rows':<46}: {total}")

        if flags:
            self.stdout.write(head(f"\nFlagged rows ({len(flags)}):"))
            for cid, bucket, note in flags:
                self.stdout.write(f"  [{bucket}] {cid}: {note}")

        if self.blocked:
            self.stdout.write(self.style.ERROR(
                "\nNOT APPLIED: rolled back because warnings exist. Review the "
                "warning block above, then re-run with --apply --force to commit "
                "(the not-cleanly-active clients will still be left untouched)."
            ))
        elif apply:
            self.stdout.write(self.style.SUCCESS(
                "\nAPPLIED (committed)" + (" [--force]" if force else "") + "."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: rolled back. Re-run with --apply to commit."
            ))
