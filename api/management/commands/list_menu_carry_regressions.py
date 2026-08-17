"""Read-only audit: list clients whose VERIFIED menu/dietary was reset by a
governing-case replacement that reused a placeholder enrollment.

Signature of the bug: a survivor enrollment ``supersedes`` a closed enrollment
whose ``close_reason == 'case_replaced'`` (the verified/serving one), and for the
same member the closed source carried a non-blank ``menu_type`` that DIFFERS from
the survivor's (e.g. Halal -> Standard). Prints one client_id per line so an agent
can visit them; makes NO changes. The fix/repair is a separate command.
"""

from django.core.management.base import BaseCommand

from api.models import EnrollmentVerification


class Command(BaseCommand):
    help = (
        "Print client IDs whose verified menu/dietary was reset by a "
        "governing-case replacement (read-only; no changes)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--details", action="store_true",
            help="Also print the menu transition + enrollment ids per client.",
        )

    def handle(self, *args, **opts):
        details = opts["details"]
        survivors = (
            EnrollmentVerification.objects
            .filter(supersedes__isnull=False)
            .select_related("supersedes")
            .prefetch_related("member_profiles", "supersedes__member_profiles")
        )
        affected = {}  # client_id -> [(menu_from, menu_to, old_enr, new_enr)]
        transitions = {}
        for e_new in survivors.iterator(chunk_size=500):
            e_old = e_new.supersedes
            if not e_old or (e_old.close_reason or "") != "case_replaced":
                continue
            oldp = {p.client_id: p for p in e_old.member_profiles.all()}
            for pn in e_new.member_profiles.all():
                po = oldp.get(pn.client_id)
                if po is None:
                    continue
                om = (po.menu_type or "").strip()
                nm = (pn.menu_type or "").strip()
                if om and om.lower() != nm.lower():
                    affected.setdefault(str(pn.client_id), []).append(
                        (om, nm or "(blank)", e_old.pk, e_new.pk)
                    )
                    key = (om, nm or "(blank)")
                    transitions[key] = transitions.get(key, 0) + 1

        # Columns: client_id, CURRENT menu type (the survivor/new enrollment --
        # what the member has now) and the OLD menu type (from the closed/old
        # enrollment -- what it should have carried). One row per affected member.
        header = f"{'client_id':<38}{'current_menu_type':<20}old_menu_type"
        if details:
            header += "    (enrollments)"
        self.stdout.write(header)
        for cid in sorted(affected):
            for om, nm, old_id, new_id in affected[cid]:
                row = f"{cid:<38}{nm:<20}{om}"
                if details:
                    row += f"    (enr {old_id} -> {new_id})"
                self.stdout.write(row)

        self.stdout.write("")
        for (om, nm), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {om} -> {nm}: {n}")
        self.stdout.write(self.style.SUCCESS(f"\n{len(affected)} affected clients."))
