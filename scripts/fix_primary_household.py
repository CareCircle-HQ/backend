"""One-off prod repair: ensure a client who holds their own Internal Service
case is the PRIMARY of their own household.

WHY: a client added as a relative's dependent BEFORE their own Internal Service
case existed stays a NON-primary member -- the case save (which calls
`ensure_primary_of_own_household` after the code fix) never ran for them. This
splits such a client out into their own household as primary, mirroring the
runtime helper.

USAGE (from the backend/ directory, against the target environment's settings):

    # 1) DRY RUN -- prints the current state + what WOULD change, no writes:
    .venv/bin/python manage.py shell -c "import scripts.fix_primary_household as f; f.run('e39915ff-6902-4ecc-acd4-3e3ad6923dcb')"

    # 2) APPLY -- performs the split inside a transaction:
    .venv/bin/python manage.py shell -c "import scripts.fix_primary_household as f; f.run('e39915ff-6902-4ecc-acd4-3e3ad6923dcb', apply=True)"

Safe to re-run: a client who is already primary (or has no household) is a no-op.
"""
from django.db import transaction

from api.models import Case, CaseType, Client, HouseholdMember


def _describe(client):
    print(f"client: {client.client_id} {client.first_name} {client.last_name}")
    membership = (
        HouseholdMember.objects.filter(client=client)
        .select_related("household")
        .first()
    )
    if membership is None:
        print("  household membership: NONE")
    else:
        hh = membership.household
        print(f"  household: {hh.household_id} ({hh.name!r})  is_primary={membership.is_primary}")
        for m in hh.members.select_related("client").all():
            print(f"     member: {m.client_id} primary={m.is_primary} "
                  f"({m.client.first_name} {m.client.last_name})")
    internal = Case.objects.filter(client=client, case_type=CaseType.INTERNAL_SERVICE)
    print(f"  internal-service cases: {internal.count()}")
    for c in internal:
        print(f"     case: {c.case_id} status={c.case_status} program={c.program_name!r}")
    return membership


def run(client_id, *, apply=False):
    client = Client.objects.filter(client_id=client_id).first()
    if client is None:
        print(f"NO CLIENT with id {client_id}")
        return

    print("=== BEFORE ===")
    membership = _describe(client)

    if membership is not None and membership.is_primary:
        print("\nNo change needed: client is already PRIMARY of their household.")
        return
    if membership is None:
        print("\nClient has no household -- nothing to split. "
              "Re-save their Internal Service case to create one, or use "
              "ensure_primary_of_own_household at runtime.")
        return

    print("\nWOULD split this client out of the shared household into their own "
          "household as PRIMARY (detaching their dietary profile from the old "
          "household's enrollments).")

    if not apply:
        print("\nDRY RUN -- no changes written. Re-run with apply=True to apply.")
        return

    from api.serializers import ensure_primary_of_own_household

    with transaction.atomic():
        new_hh = ensure_primary_of_own_household(client)
    print(f"\nAPPLIED -- client is now primary of household {new_hh.household_id}.")
    print("\n=== AFTER ===")
    _describe(client)
