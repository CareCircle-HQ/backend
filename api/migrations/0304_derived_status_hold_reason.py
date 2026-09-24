"""A hold reason for a state CARRIED OVER rather than newly decided.

    Kept On Hold: the prior household was paused; a new governing case must not
    auto-resume service. Flagged Need Review.

607 such events, 79 still held. They were landing in Uncategorized, which is wrong
in a way worth naming: Uncategorized means "nobody recorded why", whereas these have
a precise and deliberate reason -- the hold was INHERITED from the household's prior
state so that a new governing case could not silently put a paused member back into
service.

``resume_policy = manual``. Nothing will clear it automatically, and that is the
entire point of the mechanism -- but an agent CAN resume once they have reviewed the
member, unlike the reasons where no resume is possible at all. Those are "none".
"""
from django.db import migrations

CODE = "derived_status"


def seed(apps, schema_editor):
    HoldReason = apps.get_model("api", "HoldReason")
    obj, created = HoldReason.objects.update_or_create(
        code=CODE,
        defaults={
            "label": "Derived Status",
            "resume_policy": "manual",
            "resume_detail": (
                "The hold was carried over from the household's prior state. An "
                "agent resumes after reviewing the member."
            ),
            "is_system": True,
            "sort_order": 13,
            "is_active": True,
        },
    )
    print(f"  hold reason {CODE}: {'created' if created else 'updated'}")

    # Reclassify the holds already carrying this note. Scoped to the note, so a hold
    # an agent has since explained for another reason is not overwritten.
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    StageEvent = apps.get_model("api", "StageEvent")

    events = StageEvent.objects.filter(
        to_stage="on_hold", note__startswith="Kept On Hold",
    )
    stamped = events.update(hold_reason=obj)

    # And the CURRENT reason, for the ones still held -- only where it is
    # Uncategorized, so a deliberate later assignment survives.
    held = EnrollmentVerification.objects.filter(
        stage="on_hold",
        stage_events__to_stage="on_hold",
        stage_events__note__startswith="Kept On Hold",
    ).filter(hold_reason__code="uncategorized").distinct()
    moved = 0
    for enrollment in held:
        enrollment.hold_reason = obj
        enrollment.save(update_fields=["hold_reason"])
        moved += 1
    print(f"  reclassified {stamped} hold event(s), {moved} held enrollment(s)")


def unseed(apps, schema_editor):
    HoldReason = apps.get_model("api", "HoldReason")
    HoldReason.objects.filter(code=CODE).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("api", "0303_wrong_case_ticket_types")]
    operations = [migrations.RunPython(seed, unseed)]
