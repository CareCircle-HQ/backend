"""Unite Us person-migration reconciliation.

When Unite Us migrates a person to a NEW canonical id, ``GET /people/<old>``
returns a 301 and the person's cases re-parent to the new id. Our daily pull /
CSV import then creates a NEW Client (holding the imported cases + fresh profile
data) while the OLD Client keeps our internal SERVICE state (enrollment,
household, delivery calendar, member dietary profiles, agent tags/notes). That
tears ``enrollment.client`` from ``enrollment.case.client``.

``merge_migrated_client`` consolidates the two onto the NEW (surviving) client:
the new id is canonical going forward, so we MOVE the service/history/agent
state off the old record onto the new one, keep the new record's authoritative
imported profile data (moving the old record's copies only when the new one has
none), stamp ``new.migrated_from_id = old`` so we never re-create the duplicate,
then delete the emptied old client + its now-empty household.
"""
from django.db import transaction

# Tag stamped on the surviving client after a Unite Us person-migration merge so
# migrated members are easy to filter/audit. (The tag row is managed in Settings
# > Tags; get_or_create keeps this safe if it's ever missing.)
MIGRATED_TAG_NAME = "Migrated"


def resolve_client(client_id):
    """Resolve a Unite Us person id to the SURVIVING client: the client with
    that ``client_id``, or -- if that id was migrated away -- the client that
    absorbed it (``migrated_from_id``). Returns the Client or None."""
    from api.models import Client

    if not client_id:
        return None
    c = Client.objects.filter(client_id=client_id).first()
    if c is not None:
        return c
    return Client.objects.filter(migrated_from_id=str(client_id)).first()


def _summarize(old_client, new_client):
    """Counts of what a merge WOULD move (for the dry-run preview)."""
    return {
        "cases": old_client.cases.count(),
        "enrollments": old_client.enrollments.count(),
        "member_profiles": old_client.member_profiles.count(),
        "notes": old_client.notes.count(),
        "tags": old_client.tags.count(),
        "stage_events": old_client.stage_events.count(),
        "timeline_events": old_client.timeline_events.count(),
    }


# Profile relations the NEW client owns authoritatively (fresh import). We move
# the OLD copies ONLY when the new record has none, otherwise the old (stale)
# copies are dropped so we don't duplicate insurance/address/screening rows.
_PROFILE_MOVE_IF_ABSENT = (
    "insurances", "addresses", "phones", "social_care_coverages",
    "screenings", "assessments",
)


def merge_migrated_client(old_client, new_client, *, actor_label="", dry_run=False):
    """Merge ``old_client`` into ``new_client`` (NEW survives). Returns a summary
    dict; on ``dry_run`` reports what WOULD move without writing.

    Idempotent-ish: a no-op when old == new. Callers should pass real, distinct
    Client instances (use :func:`resolve_client`)."""
    from django.utils import timezone

    from api.models import (
        ClientTag, Household, MemberDietaryProfile, MilitaryProfile, Note,
        TimelineEventType, Ticket,
    )
    from api.serializers import ensure_household_with_primary
    from api.services.timeline import emit_timeline_event

    if old_client is None or new_client is None:
        return {"merged": False, "reason": "missing client"}
    if old_client.pk == new_client.pk:
        return {"merged": False, "reason": "same client"}

    summary = {"merged": not dry_run, "old_id": str(old_client.client_id),
               "new_id": str(new_client.client_id), **_summarize(old_client, new_client)}
    if dry_run:
        return summary

    with transaction.atomic():
        # The surviving client's own household (create if missing); moved
        # enrollments are re-homed here so enrollment.client == its household's
        # primary again.
        new_hh = ensure_household_with_primary(new_client)

        # Capture the old client's household before we detach it.
        old_membership = getattr(old_client, "household_membership", None)
        old_hh = old_membership.household if old_membership is not None else None

        # --- MOVE service / history / agent state onto the survivor ---
        # Cases (case_id is unique -> a straight reassign, no duplicates).
        old_client.cases.all().update(client=new_client)
        # Enrollments -> new client, re-homed under the survivor's household.
        for enr in old_client.enrollments.all():
            enr.client = new_client
            enr.household = new_hh
            enr.save(update_fields=["client", "household"])
        # Member dietary profiles (SET_NULL FK) -> new client.
        MemberDietaryProfile.objects.filter(client=old_client).update(client=new_client)
        # Append-only history + agent items -> new client.
        old_client.stage_events.all().update(client=new_client)
        old_client.timeline_events.all().update(client=new_client)
        old_client.warnings.all().update(client=new_client)
        old_client.case_mismatch_flags.all().update(client=new_client)
        Note.objects.filter(client=old_client).update(client=new_client)
        Ticket.objects.filter(client=old_client).update(client=new_client)
        # Tags: union onto the survivor.
        for tag in old_client.tags.all():
            new_client.tags.add(tag)

        # --- Profile data: keep the survivor's; move the old only if absent ---
        for rel in _PROFILE_MOVE_IF_ABSENT:
            new_mgr = getattr(new_client, rel)
            old_mgr = getattr(old_client, rel)
            if new_mgr.exists():
                # Survivor already holds authoritative import data -> drop stale.
                old_mgr.all().delete()
            else:
                old_mgr.all().update(client=new_client)
        # Military profile is OneToOne: move only if the survivor lacks one.
        old_mil = MilitaryProfile.objects.filter(client=old_client).first()
        if old_mil is not None:
            if MilitaryProfile.objects.filter(client=new_client).exists():
                old_mil.delete()
            else:
                old_mil.client = new_client
                old_mil.save(update_fields=["client"])

        # --- Stamp the alias + "Migrated" tag + tear down the old record ---
        new_client.migrated_from_id = str(old_client.client_id)
        new_client.save(update_fields=["migrated_from_id"])
        mig_tag, _ = ClientTag.objects.get_or_create(name=MIGRATED_TAG_NAME)
        new_client.tags.add(mig_tag)

        # Record the migration on the survivor's timeline (old id -> new id) so
        # the History tab explains why the records were consolidated.
        old_id_str = str(old_client.client_id)
        new_id_str = str(new_client.client_id)
        try:
            emit_timeline_event(
                client=new_client,
                event_type=TimelineEventType.CLIENT_MIGRATED,
                occurred_at=timezone.now(),
                title="Client migrated (Unite Us)",
                subtitle=(
                    f"Unite Us migrated this member to a new id; records "
                    f"consolidated {old_id_str} \u2192 {new_id_str}."
                ),
                source="system",
                actor=actor_label or "",
                metadata={"old_id": old_id_str, "new_id": new_id_str},
            )
        except Exception:  # pragma: no cover - never let history logging block the merge
            pass

        # Detach + delete the old client (CASCADE removes only leftover derived
        # rows -- eligibilities / display logs / login codes -- that the survivor
        # regenerates). Then drop the old household if it's now empty.
        # Re-home EVERYTHING still pointing at the OLD household onto the
        # survivor's household before deleting either -- some FKs to Household are
        # PROTECT (e.g. OrderSchedule.household), so a stale reference would block
        # the delete. Skip HouseholdMember (the old client's membership cascades
        # when the client is deleted).
        from api.models import HouseholdMember as _HouseholdMember

        if old_hh is not None and old_hh.pk != new_hh.pk:
            for rel in old_hh._meta.related_objects:
                if rel.many_to_many:
                    continue
                rel_model = rel.related_model
                if rel_model is _HouseholdMember:
                    continue
                fname = rel.field.name
                rel_model.objects.filter(**{fname: old_hh}).update(**{fname: new_hh})

        old_client.delete()
        if old_hh is not None and old_hh.pk != new_hh.pk:
            if not _HouseholdMember.objects.filter(household=old_hh).exists():
                Household.objects.filter(pk=old_hh.pk).delete()

    # Post-merge: with the OLD client's cases now consolidated onto the survivor
    # (alongside the ones the import already re-parented to the new id), re-run
    # the governing internal-service case detection so the CORRECT governing case
    # is chosen per our normal rules (governing_case_key) and projected onto the
    # enrollment -- then recompute the funnel stage.
    try:
        from api.services.lifecycle import (
            reconcile_internal_service_authorization, recompute_client_stage,
        )
        reconcile_internal_service_authorization(new_client, actor_label=actor_label)
        recompute_client_stage(new_client)
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from api.services.orders import rebuild_delivery_calendar
        for enr in new_client.enrollments.all():
            rebuild_delivery_calendar(enr)
    except Exception:  # pragma: no cover - defensive
        pass

    return summary
