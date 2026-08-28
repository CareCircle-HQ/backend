"""Tag the WHOLE household of every client listed in a CSV with the
"9/1 HH CLOSE" ClientTag (the tag already exists on local + prod).

The CSV has a ``client_id`` column (e.g. tmp/import/OpenHouseholdMemberstotag.csv).
For each listed client we tag every CURRENT member of their household -- not just
the listed row -- so a whole closing household is tagged even if the file misses a
member. Clients with no household record are tagged on their own.

Review-only by default:
    python manage.py tag_hh_close
Apply:
    python manage.py tag_hh_close --apply
Custom file:
    python manage.py tag_hh_close --file path/to/file.csv --apply
"""
import csv

from django.core.management.base import BaseCommand

from api.models import Client, ClientTag, HouseholdMember

TAG_NAME = "9/1 HH CLOSE"
DEFAULT_FILE = "tmp/import/OpenHouseholdMemberstotag.csv"


class Command(BaseCommand):
    help = f"Tag whole households from a CSV with the {TAG_NAME!r} tag."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=DEFAULT_FILE)
        parser.add_argument("--apply", action="store_true", help="Commit (default: review only).")

    def handle(self, *args, **options):
        tag = ClientTag.objects.filter(name=TAG_NAME).first()
        if tag is None:
            self.stderr.write(self.style.ERROR(f"Tag {TAG_NAME!r} not found -- create it first."))
            return

        # 1) Read the seed client_ids from the CSV.
        seed_ids = []
        with open(options["file"], newline="") as f:
            for row in csv.DictReader(f):
                cid = (row.get("client_id") or "").strip()
                if cid:
                    seed_ids.append(cid)
        unique_seed = set(seed_ids)

        # 2) Which seeds actually exist as clients.
        found = set(
            str(x) for x in Client.objects.filter(client_id__in=unique_seed)
            .values_list("client_id", flat=True)
        )
        missing = sorted(unique_seed - found)

        # 3) Expand to the WHOLE household of every found seed (current members),
        #    keeping household-less seeds too.
        household_ids = set(
            HouseholdMember.objects.filter(client_id__in=found)
            .values_list("household_id", flat=True)
        )
        member_ids = set(found)
        member_ids |= set(
            str(x) for x in HouseholdMember.objects.filter(household_id__in=household_ids)
            .values_list("client_id", flat=True)
        )
        to_tag = Client.objects.filter(client_id__in=member_ids)
        total = to_tag.count()
        already = to_tag.filter(tags=tag).count()
        new = total - already

        # Report.
        self.stdout.write(f"CSV rows with a client_id : {len(seed_ids)} ({len(unique_seed)} unique)")
        self.stdout.write(f"  resolved to a client    : {len(found)}")
        self.stdout.write(f"  NOT found (skipped)     : {len(missing)}")
        if missing:
            self.stdout.write("    sample: " + ", ".join(missing[:5]))
        self.stdout.write(f"households expanded       : {len(household_ids)}")
        self.stdout.write(f"members to tag (whole HH) : {total}")
        self.stdout.write(f"  already tagged          : {already}")
        self.stdout.write(f"  NEW to tag              : {new}")

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("Review only. Re-run with --apply to tag.")
            return

        # add(*objs) is a single bulk insert and ignores members already tagged.
        tag.clients.add(*list(to_tag))
        self.stdout.write(self.style.SUCCESS(f"Tagged {total} member(s) with {TAG_NAME!r} ({new} new)."))
