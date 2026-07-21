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

from api.models import (
    Case,
    CaseType,
    Client,
    HouseholdMember,
    MemberDietaryProfile,
)


def _member_is_primary(client):
    """Whether ``client`` is the PRIMARY of their own roster household."""
    membership = getattr(client, "household_membership", None)
    if membership is None:
        membership = HouseholdMember.objects.filter(client=client).first()
    return bool(getattr(membership, "is_primary", False)), membership


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
    _describe_enrollments(client, membership)
    return membership


def _describe_enrollments(client, membership):
    """Dump the enrollments that govern this client + their per-member dietary
    profiles, flagging WHICH profile the Household/Program tab would render as
    'primary' (the one whose owner is is_primary of their OWN roster household).

    This is where a mismatch hides: the roster can say the client is primary of
    their solo household, yet their governing enrollment is still the OLD shared
    household's -- so its member_profiles list a DIFFERENT member (the old
    primary), which is what the Program tab shows.
    """
    own = [e for e in client.enrollments.all() if e.stage != "disregarded"]
    print(f"  own enrollments (non-disregarded): {len(own)}")
    for e in own:
        print(f"     enr {e.pk} code={e.code!r} stage={e.stage} "
              f"closed={e.closed_at is not None} household={e.household_id} "
              f"client={e.client_id}")

    # Which enrollment the Program/Household tab actually renders (mirrors
    # portal.serializers.active_enrollment: own first, else household's).
    governing = own
    if not governing and membership is not None:
        governing = [
            e for e in membership.household.enrollment_verifications.all()
            if e.stage != "disregarded"
        ]
        print(f"  NO own enrollment -> falls back to household "
              f"{membership.household_id}'s enrollments: {len(governing)}")
    if not governing:
        print("  governing enrollment: NONE (Household/Program tab would be empty)")
        return
    open_ones = [e for e in governing if e.closed_at is None]
    pool = open_ones or governing
    active = sorted(pool, key=lambda e: e.opened_at or e.pk, reverse=True)[0]
    print(f"  >>> ACTIVE enrollment (drives Program tab): {active.pk} "
          f"stage={active.stage} household={active.household_id}")
    profiles = (
        MemberDietaryProfile.objects.filter(enrollment=active)
        .select_related("client__household_membership")
    )
    print(f"      member profiles: {profiles.count()}")
    for p in profiles:
        is_primary, _m = (
            _member_is_primary(p.client) if p.client_id else (False, None)
        )
        flag = "  <== shown as PRIMARY" if is_primary else ""
        pname = p.member_name or (
            f"{p.client.first_name} {p.client.last_name}".strip()
            if p.client else ""
        )
        print(f"        profile {p.pk} client={p.client_id} "
              f"roster_primary={is_primary} status={p.status} ({pname}){flag}")


def diagnose(client_id):
    """READ-ONLY: print the full roster + enrollment/profile picture for a
    client, to pinpoint why the Program tab shows the wrong primary. No writes."""
    client = Client.objects.filter(client_id=client_id).first()
    if client is None:
        print(f"NO CLIENT with id {client_id}")
        return
    print("=== DIAGNOSE ===")
    _describe(client)


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
