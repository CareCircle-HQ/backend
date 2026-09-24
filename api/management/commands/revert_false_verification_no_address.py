"""Revert the 17 falsely-verified members who had NO delivery address.

A pinned-list wrapper around ``revert_falsely_verified_enrollments``. It exists so the
scope travels with the repo instead of living in a /tmp file somebody has to recreate
on the box, and so it cannot be run with the wrong scope by accident.

WHY THESE MEMBERS
-----------------
``reconcile_member_stages`` treated an APPROVED authorization as proof of verification
and stamped ``verified_at`` itself, advancing members past the verification pop-up AND
the nutritionist step. Two runs did it -- 06 Jul 2026 02:47 and 21 Aug 2026 18:27 --
leaving the note ``Reconcile: approved but delivery data incomplete.`` on 1,594 stage
events across 1,548 clients. The code was fixed in ``0a61650`` on 25 Aug, four days
after the second run; the data it had already written stayed.

The symptom an agent sees is a member reading **Verified with no delivery address**,
because nobody was ever asked for one. These 17 are the subset that, on the 24 Sep
clone, were:

  * carrying the false-verification signature (``verified_at`` set, ``verified_by``
    NULL, ``nutritionist_approved_at`` NULL)
  * at ``kitchen_assignment`` -- past verification but NOT serving
  * the household PRIMARY, not a dependent
  * holding an OPEN governing internal-service case
  * and had NO delivery address at all

⚠ PINNED BY CLIENT ID, NOT RE-QUERIED. The addresses were added manually in production
on 24 Sep, so a live "no delivery address" query now returns nothing. The list is the
historical fact; re-deriving it would silently produce an empty run.

⚠ NOT scoped by ``--since``. The client list IS the scope, which avoids the risk the
parent command warns about: an unscoped ``--since`` also matches the 29 Jun Meal
Inputs bulk import, whose verifications may have been intentional.

WHAT IT DOES
------------
Delegates to ``revert_falsely_verified_enrollments`` -- no revert logic is duplicated
here, so this cannot drift from it. That command clears the false ``verified_at``,
drops the imported/carried kitchen + cadence, pulls future deliveries off the
calendar, and RECOMPUTES the client's lifecycle stage.

That last step is the one a manual stage edit misses, and it is why members edited by
hand still showed as Verified: the Verification page's Pending/Verified split is keyed
off ``verified_at`` via ``verification_completed_q()``, never the stage.

Serving rows (SERVICE_ACTIVE / ON_HOLD / SERVICE_COMPLETE) are reported and NOT
touched unless ``--include-serving`` is passed through. On the clone all 17 were at
kitchen_assignment, so nothing was serving and no deliveries would have been pulled.

USAGE
-----
    python manage.py revert_false_verification_no_address            # dry run
    python manage.py revert_false_verification_no_address --apply

Expect 17. A LOWER count is information, not a failure: production has moved since the
clone, and any member who has since advanced to SERVICE_ACTIVE is skipped as serving.
"""
import os
import tempfile

from django.core.management import call_command
from django.core.management.base import BaseCommand

# The 17, from the 24 Sep clone. Names are for the reader; only the ids are used.
CLIENT_IDS = [
    ("7f3eb3a5-f4c7-4b7f-9184-7570931fbecc", "ANGIE MARTINEZ GACHUPIN"),
    ("56692e9d-50ef-486b-87b7-4db6b13bdf47", "ANITA SHEPHERD"),
    ("ca3e110a-11c4-42d6-bebf-a4d3ceca890e", "BEATA KRANKOWSKI"),
    ("89b0d961-f819-442f-92dc-0a8c2752d5b1", "CHRISTOPHE JOHNSON"),
    ("6a80fbb5-b731-444c-8ab7-3c69af1d67d5", "CYNTHIA THOMAS"),
    ("db450d92-29d6-497c-977b-02a42872fae9", "DIANE SANTIAGO"),
    ("2042d8c2-cdb6-4bb7-bce9-8e1b9a3bf619", "ERIKA CORAIZACA TOAPANTA"),
    ("bd5154ce-ddb4-487f-ba8d-67e91d2d6402", "GREGORY GRIFFITHS"),
    ("80c62c63-c8b5-4aae-8ebc-4700c8ef10f5", "JAMES HAMM"),
    ("831165a9-1974-4cbd-bbaa-9972e128d4ad", "JULIANA RATNA"),
    ("7c9660f6-be4c-4ad0-a161-b11d981e749b", "MESSIAH RODRIGUEZ"),
    ("08f9e4e6-3aa8-494e-bf2b-8fb9841131f7", "PEARL KUBITSHUK"),
    ("548eb771-f2d0-43df-a19d-6ac1f3b26917", "PRINCESSNI LITTLE"),
    ("cc348cdf-dc31-472c-92eb-f50950ceba28", "RAMONA SHEPHERD"),
    ("14afac76-0f94-44de-af55-079517e5165e", "RANAZIA ANDERSON"),
    ("6f00187f-729b-46e4-89aa-ef4f143b2595", "TAYLOR WASHINGTON"),
    ("842603fe-ca94-4b8d-b81d-0e6bb474d33b", "WESTLEY FOSTER"),
]


class Command(BaseCommand):
    help = (
        "Revert the 17 falsely-verified members who had no delivery address "
        "(pinned list; delegates to revert_falsely_verified_enrollments)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Commit the changes.",
        )
        parser.add_argument(
            "--include-serving", action="store_true",
            help=(
                "Also revert a member who has since reached SERVICE_ACTIVE / "
                "ON_HOLD (disruptive: pulls future deliveries)."
            ),
        )

    def handle(self, *args, **options):
        self.stdout.write(
            f"Pinned scope: {len(CLIENT_IDS)} client(s) from the 24 Sep clone.\n"
        )
        # A real temp file rather than piping: --clients-file is the parent's only
        # list interface, and reusing it means the revert logic stays in ONE place.
        fd, path = tempfile.mkstemp(prefix="false_verification_", suffix=".txt")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(cid for cid, _name in CLIENT_IDS) + "\n")
            call_command(
                "revert_falsely_verified_enrollments",
                clients_file=path,
                list=True,
                apply=options["apply"],
                include_serving=options["include_serving"],
                stdout=self.stdout,
                stderr=self.stderr,
            )
        finally:
            os.unlink(path)
