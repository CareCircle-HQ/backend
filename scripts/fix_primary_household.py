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
    EnrollmentVerification,
    HouseholdMember,
    MemberDietaryProfile,
    MemberStatus,
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


def _heal_client_enrollments(client, *, apply=False):
    """Re-anchor a client's OWN enrollments to their roster household, strip
    stray foreign member profiles, and ensure the client has their own profile.

    Repairs the case where a client was made primary of their own (solo)
    household but their enrollments stayed pointing at the OLD shared household
    -- so the Program tab renders the old household's member(s) (e.g. another
    person who is primary of THEIR own household) instead of the client.

    A stray profile (owner NOT in this household's roster) is only DELETED when
    that owner has their dietary data elsewhere (another profile); otherwise it's
    left in place and flagged for manual review, so we never destroy a member's
    only dietary record. Idempotent.
    """
    membership = (
        HouseholdMember.objects.filter(client=client)
        .select_related("household")
        .first()
    )
    if membership is None:
        print("  client has no roster household -- cannot heal enrollments.")
        return
    household = membership.household
    roster_ids = set(household.members.values_list("client_id", flat=True))
    own = list(EnrollmentVerification.objects.filter(client=client))
    print(f"  healing {len(own)} own enrollment(s) into household "
          f"{household.household_id} (roster={sorted(str(i) for i in roster_ids)})")
    for enr in own:
        # 1) Re-anchor the enrollment to the client's roster household.
        if enr.household_id != household.household_id:
            print(f"    enr {enr.pk}: household {enr.household_id} -> "
                  f"{household.household_id}")
            if apply:
                enr.household = household
                enr.save(update_fields=["household"])
        # 2) Strip stray profiles whose owner isn't in this household's roster,
        #    but only when that owner has their dietary data elsewhere.
        for p in list(enr.member_profiles.select_related("client").all()):
            if not p.client_id or p.client_id in roster_ids:
                continue
            has_data_elsewhere = (
                MemberDietaryProfile.objects.filter(client_id=p.client_id)
                .exclude(pk=p.pk)
                .exists()
            )
            if has_data_elsewhere:
                print(f"    enr {enr.pk}: strip stray profile {p.pk} "
                      f"(client={p.client_id}; has data elsewhere)")
                if apply:
                    p.delete()
            else:
                print(f"    enr {enr.pk}: SKIP stray profile {p.pk} "
                      f"(client={p.client_id}) -- it's their ONLY profile; "
                      f"needs manual review")
        # 3) Ensure the client has their OWN profile on this enrollment (theirs
        #    was deleted when they were split out). Starts Out of Orbit until a
        #    menu type + dietary needs are set (mirrors sync_household_members).
        if not enr.member_profiles.filter(client=client).exists():
            print(f"    enr {enr.pk}: create own profile for {client.client_id}")
            if apply:
                MemberDietaryProfile.objects.create(
                    enrollment=enr, client=client,
                    member_name=f"{client.first_name} {client.last_name}".strip(),
                    menu_type="", status=MemberStatus.OUT_OF_ORBIT,
                )


def heal(client_id, *, apply=False):
    """Fix a client whose enrollments are stranded on the OLD shared household.

        # DRY RUN (default) -- prints what WOULD change, no writes:
        .venv/bin/python manage.py shell -c "import scripts.fix_primary_household as f; f.heal('<client_id>')"

        # APPLY -- performs the repair inside a transaction:
        .venv/bin/python manage.py shell -c "import scripts.fix_primary_household as f; f.heal('<client_id>', apply=True)"
    """
    client = Client.objects.filter(client_id=client_id).first()
    if client is None:
        print(f"NO CLIENT with id {client_id}")
        return
    print("=== BEFORE ===")
    _describe(client)
    print("\n=== HEAL ===")
    if apply:
        with transaction.atomic():
            _heal_client_enrollments(client, apply=True)
        print("\nAPPLIED.")
        print("\n=== AFTER ===")
        _describe(client)
    else:
        _heal_client_enrollments(client, apply=False)
        print("\nDRY RUN -- no changes written. Re-run with apply=True to apply.")


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
