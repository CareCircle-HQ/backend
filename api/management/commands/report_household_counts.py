"""Report internal-service 'household' clients whose household isn't built out.

Two read-only steps:

  1. Find every client with an INTERNAL_SERVICE case whose ``program_name``
     contains the word "household".
  2. Keep only those whose CURRENT household has just one member (``--members``,
     default 1) -- i.e. only the client themselves is set up, so the rest of the
     household still needs to be added.

For each matched client we print:

    client_id | name | current_household_members | internal_cases | program

``current_household_members`` is how many HouseholdMember rows currently exist
(0 = no household group at all, 1 = just the client). ``program`` shows the
"household" program name(s) that matched. Nothing is written.

Usage:
    python manage.py report_household_counts
    python manage.py report_household_counts --members 1   # cap (default 1)
"""

from django.core.management.base import BaseCommand

from api.models import Case, CaseType, Client


class Command(BaseCommand):
    help = (
        "List internal-service clients whose program name contains 'household' "
        "but whose household still has only 1 member (needs building out)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--members", type=int, default=1,
            help="Only include clients whose household has this many members or fewer (default 1).",
        )

    def handle(self, *args, **options):
        max_members = options["members"]

        # Step 1: internal-service cases whose program name mentions 'household'.
        program_names = {}
        case_counts = {}
        for cid, pname in (
            Case.objects.filter(
                case_type=CaseType.INTERNAL_SERVICE,
                program_name__icontains="household",
            )
            .values_list("client_id", "program_name")
        ):
            if not cid:
                continue
            program_names.setdefault(cid, set()).add(pname)
            case_counts[cid] = case_counts.get(cid, 0) + 1

        clients = (
            Client.objects.filter(pk__in=program_names.keys())
            .prefetch_related("household_membership__household__members")
        )

        # Step 2: keep only those whose household has <= max_members members.
        rows = []
        for client in clients:
            membership = getattr(client, "household_membership", None)
            household = getattr(membership, "household", None)
            member_count = household.members.count() if household else 0
            if member_count > max_members:
                continue
            name = f"{(client.first_name or '').strip()} {(client.last_name or '').strip()}".strip()
            rows.append((
                str(client.pk), name, member_count,
                case_counts[client.pk], "; ".join(sorted(program_names[client.pk])),
            ))

        rows.sort(key=lambda r: r[1].lower())

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== 'household' internal-service clients with <= {max_members} "
            f"household member(s) ==="
        ))
        self.stdout.write(
            f"{'client_id':<38} {'name':<28} {'members':>8} {'cases':>6}  program"
        )
        for cid, name, member_count, cases, program in rows:
            self.stdout.write(
                f"{cid:<38} {name[:28]:<28} {member_count:>8} {cases:>6}  {program}"
            )
        self.stdout.write(self.style.SUCCESS(f"\nTOTAL matched clients: {len(rows)}"))
