"""One-off data fix (2026-07-23): repair MemberDietaryProfile rows that are
attached to the WRONG client.

A "mislinked" profile is a ``MemberDietaryProfile`` whose ``client`` is neither
the enrollment's own client NOR a member of the enrollment's household. It comes
from a household split (``ensure_primary_of_own_household``) that moved a client
out of a shared household but left the OTHER member's dietary profile behind on
this enrollment (see the "stray profiles" note in api/serializers.py). The stray
profile then surfaces on the primary's Household tab / out-of-service roll-up
(and can flip the primary to "Out of Orbit" in the UI) even though the primary's
own ``member_out_of_orbit`` -- and every CSV export -- says otherwise.

Each stray is categorised by what the stray client (R) has of their OWN:

  A. delete-safe  -- R already has a dietary profile on their OWN enrollment
                     (client == R). The stray here is a redundant leftover ->
                     DELETE it.
  B. rehome       -- R has an enrollment of their own but no profile on it. The
                     stray IS R's only dietary data -> MOVE it onto R's own
                     enrollment (only with --rehome; never auto).
  C. manual       -- R has no enrollment of their own. The stray is R's only
                     dietary data and there's nowhere safe to move it -> REPORT
                     ONLY (a human must decide: re-add R to the household roster,
                     or delete).

Dry-run by default (prints what WOULD change and writes a full CSV). Pass
--apply to commit. --apply performs category A deletions; add --rehome to also
move category B. Category C is never mutated. Everything runs in one
transaction and is idempotent. Safe to delete this command once run on prod.
"""
import csv

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import (
    EnrollmentVerification,
    HouseholdMember,
    MemberDietaryProfile,
)


def _stray_profiles():
    """Return every mislinked MemberDietaryProfile (profile.client is not the
    enrollment's client and not a member of the enrollment's household)."""
    strays = []
    qs = (
        MemberDietaryProfile.objects
        .select_related("enrollment", "client")
        .filter(client_id__isnull=False)
    )
    # Cache household rosters we've already looked up to keep this cheap.
    roster_cache = {}
    for p in qs:
        e = p.enrollment
        if e is None or not e.client_id:
            continue
        if str(p.client_id) == str(e.client_id):
            continue  # the enrollment's own client -> correct
        hh_id = e.household_id
        if hh_id is not None:
            members = roster_cache.get(hh_id)
            if members is None:
                members = set(
                    HouseholdMember.objects
                    .filter(household_id=hh_id)
                    .values_list("client_id", flat=True)
                )
                roster_cache[hh_id] = members
            if p.client_id in members:
                continue  # a real household member -> correct
        strays.append(p)
    return strays


def _own_enrollments(client_id):
    """The stray client's OWN enrollments (enrollment.client == them), newest
    open first, then newest overall -- mirrors active_enrollment's ordering."""
    enrs = [
        e for e in EnrollmentVerification.objects.filter(client_id=client_id)
        if e.stage != "disregarded"
    ]
    enrs.sort(key=lambda e: (e.closed_at is None, e.opened_at or e.created_at), reverse=True)
    return enrs


def _categorise(strays):
    """Split strays into (A_delete, B_rehome, C_manual) lists of dicts."""
    A, B, C = [], [], []
    for p in strays:
        r = p.client_id
        own_enrs = _own_enrollments(r)
        own_enr_ids = [e.pk for e in own_enrs]
        has_own_profile = bool(own_enr_ids) and MemberDietaryProfile.objects.filter(
            client_id=r, enrollment_id__in=own_enr_ids
        ).exists()
        row = {
            "profile": p,
            "own_enrs": own_enrs,
            "has_own_profile": has_own_profile,
        }
        if has_own_profile:
            A.append(row)
        elif own_enrs:
            B.append(row)
        else:
            C.append(row)
    return A, B, C


class Command(BaseCommand):
    help = (
        "Repair MemberDietaryProfile rows linked to the wrong client. Dry-run "
        "by default; --apply deletes the redundant (category A) strays, "
        "--rehome also moves category B onto the stray client's own enrollment."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply. Without this the command only previews.",
        )
        parser.add_argument(
            "--rehome", action="store_true",
            help="Also move category B strays onto the stray client's own "
                 "enrollment (only meaningful with --apply).",
        )
        parser.add_argument(
            "--csv", default="",
            help="Optional path to write the full categorised stray report.",
        )

    def _fmt(self, p):
        c = p.client
        pc = f"{c.first_name} {c.last_name}".strip() if c else "?"
        e = p.enrollment
        ec = e.client if e else None
        en = f"{ec.first_name} {ec.last_name}".strip() if ec else "?"
        return (
            f"profile {p.id} status={p.status} menu={p.menu_type!r} | "
            f"stray client {str(p.client_id)[:8]} ({pc}) "
            f"on enrollment {e.pk} of {str(e.client_id)[:8]} ({en})"
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        rehome = opts["rehome"]

        strays = _stray_profiles()
        A, B, C = _categorise(strays)
        self.stdout.write(
            f"Found {len(strays)} mislinked profile(s): "
            f"{len(A)} delete-safe (A), {len(B)} rehome (B), {len(C)} manual (C)."
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\nA) DELETE-SAFE (redundant leftovers):"))
        for row in A:
            self.stdout.write("  " + self._fmt(row["profile"]))
        self.stdout.write(self.style.MIGRATE_HEADING("\nB) REHOME (stray is only data, R has an enrollment):"))
        for row in B:
            tgt = row["own_enrs"][0].pk if row["own_enrs"] else None
            self.stdout.write(f"  -> enr {tgt}: " + self._fmt(row["profile"]))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nC) MANUAL REVIEW ({len(C)} -- NOT mutated; stray is R's only data, no own enrollment):"))
        for row in C:
            self.stdout.write("  " + self._fmt(row["profile"]))

        csv_path = opts["csv"]
        if csv_path:
            with open(csv_path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "category", "profile_id", "status", "menu_type",
                    "stray_client_id", "stray_client_name",
                    "enrollment_id", "enrollment_client_id", "enrollment_client_name",
                ])
                for cat, rows in (("A", A), ("B", B), ("C", C)):
                    for row in rows:
                        p = row["profile"]
                        c = p.client
                        e = p.enrollment
                        ec = e.client if e else None
                        w.writerow([
                            cat, p.id, p.status, p.menu_type,
                            p.client_id, f"{c.first_name} {c.last_name}".strip() if c else "",
                            e.pk if e else "", e.client_id if e else "",
                            f"{ec.first_name} {ec.last_name}".strip() if ec else "",
                        ])
            self.stdout.write(self.style.SUCCESS(f"\nWrote full report to {csv_path}"))

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- no changes made. Re-run with --apply to commit "
                "(category A deletions; add --rehome for category B)."
            ))
            return

        deleted = 0
        rehomed = 0
        with transaction.atomic():
            for row in A:
                row["profile"].delete()
                deleted += 1
            if rehome:
                for row in B:
                    p = row["profile"]
                    target = row["own_enrs"][0]
                    # Guard the (enrollment, client) unique constraint.
                    if MemberDietaryProfile.objects.filter(
                        enrollment=target, client_id=p.client_id
                    ).exists():
                        p.delete()
                        deleted += 1
                    else:
                        p.enrollment = target
                        p.save(update_fields=["enrollment", "updated_at"])
                        rehomed += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: deleted {deleted} stray profile(s), rehomed {rehomed}."
        ))
        if not rehome and B:
            self.stdout.write(self.style.WARNING(
                f"{len(B)} category-B stray(s) left untouched (pass --rehome to move them)."
            ))
        if C:
            self.stdout.write(self.style.WARNING(
                f"{len(C)} category-C stray(s) need manual review -- see the CSV."
            ))
