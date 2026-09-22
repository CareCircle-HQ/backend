"""Apply the "Voucher Case tag" to the members holding a Voucher Food Prescription
case.

    python manage.py tag_voucher_cases            # dry run, changes nothing
    python manage.py tag_voucher_cases --apply

⚠ TAGS LIVE ON THE MEMBER, NOT THE CASE. ``ClientTag`` is a many-to-many on
``Client``; there is no tag field on ``Case``. So "tag these cases" is carried out
as "tag the members who hold one of these cases", which is the only thing the data
model supports. It follows that a member tagged here may also hold cases that have
nothing to do with vouchers -- the tag says "this member has a voucher case", not
"this case is a voucher case".

The programme list is EXPLICIT rather than a pattern match. A ``Voucher`` wildcard
would also pick up the 12 "Reauthorization: … Voucher" programmes, which were not
asked for, and any programme added later would silently join the set.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

TAG_NAME = "Voucher Case tag"

PROGRAM_NAMES = [
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - (Household) Pregnant / Postpartum - Manhattan",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - Pregnant / Postpartum - Manhattan",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - (Household) High-Risk Children Under the Age of 18 - Manhattan",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - (Household) High-Risk Children Under the Age of 18 - Brooklyn",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - (Household) High-Risk Children Under the Age of 18 - Queens",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - Other Eligible Populations - Manhattan",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - Other Eligible Populations - Brooklyn",
    "Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher"
    " - Other Eligible Populations - Queens",
]


class Command(BaseCommand):
    help = "Tag members who hold a Voucher Food Prescription case."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it the command only reports.",
        )

    def handle(self, *args, **options):
        from api.models import Case, ClientTag

        apply = options["apply"]

        tag = ClientTag.objects.filter(name=TAG_NAME).first()
        if tag is None:
            # NOT created here. The tag carries a colour and is managed in
            # Settings; inventing one would give production a tag that does not
            # match the one an agent already made.
            self.stderr.write(self.style.ERROR(
                f'No tag named "{TAG_NAME}". Create it in Settings > Tags first. '
                f"Existing: "
                + ", ".join(sorted(ClientTag.objects.values_list("name", flat=True)))
            ))
            return

        # Programme names are reported individually so a rename shows up as a zero
        # rather than quietly shrinking the total.
        self.stdout.write(f'Tag: "{tag.name}" ({tag.pk})')
        self.stdout.write("")
        missing = []
        client_ids = set()
        for name in PROGRAM_NAMES:
            cases = Case.objects.filter(program_name=name).exclude(client=None)
            ids = set(cases.values_list("client_id", flat=True))
            client_ids |= ids
            if not ids:
                missing.append(name)
            self.stdout.write(
                f"  {cases.count():>4} cases  {len(ids):>4} members   …{name[-58:]}"
            )

        if missing:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{len(missing)} programme(s) matched NOTHING -- check for a rename:"
            ))
            for name in missing:
                self.stdout.write(f"    {name}")

        already = set(
            tag.clients.filter(client_id__in=client_ids).values_list(
                "client_id", flat=True,
            )
        )
        to_add = client_ids - already

        self.stdout.write("")
        self.stdout.write(f"  members with a voucher case : {len(client_ids)}")
        self.stdout.write(f"  already tagged              : {len(already)}")
        self.stdout.write(f"  to tag                      : {len(to_add)}")

        if not apply:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write."
            ))
            return

        if not to_add:
            self.stdout.write(self.style.SUCCESS("\nNothing to do."))
            return

        from api.models import Client

        with transaction.atomic():
            # add() on the through table: idempotent, and it does NOT touch any
            # other tag the member already has.
            for client in Client.objects.filter(client_id__in=to_add):
                client.tags.add(tag)

        self.stdout.write(self.style.SUCCESS(f"\nTagged {len(to_add)} member(s)."))
