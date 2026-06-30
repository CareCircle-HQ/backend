"""Data migration: collapse the removed verification/authorization stages.

The verification stage is now a yes/no fact and authorization is a separate
dimension, so the EnrollmentStage values waiting_authorization / authorized /
denied and the ClientStage values waiting_authorization / authorized no longer
exist. This migration:

1. Remaps existing EnrollmentVerification rows off the removed stages:
   - authorized                       -> kitchen_assignment (approval advanced it)
   - waiting_authorization / denied   -> verified (the pop-up had been completed
     in the canonical flow; the auth outcome now lives on the Case)
2. Backfills ``verified_at`` for every verified-or-beyond enrollment that lacks
   it, using the recorded transition INTO verified (StageEvent) when available,
   else the enrollment's stage_at / opened_at.
3. Recomputes the lifecycle stage of clients still stored at a removed
   ClientStage so no invalid value survives (and so a case-authorization-only
   client, which the old funnel leak pushed to waiting_authorization/authorized,
   falls back to its real derived stage now that the leak is gone).
"""
from django.db import migrations

_VERIFIED_PLUS = [
    "verified",
    "kitchen_assignment",
    "service_active",
    "service_complete",
]


def forward(apps, schema_editor):
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    StageEvent = apps.get_model("api", "StageEvent")

    # 1. Remap removed enrollment stages.
    EnrollmentVerification.objects.filter(stage="authorized").update(
        stage="kitchen_assignment"
    )
    EnrollmentVerification.objects.filter(
        stage__in=["waiting_authorization", "denied"]
    ).update(stage="verified")

    # 2. Backfill verified_at for verified-or-beyond enrollments (and any on_hold
    #    enrollment that passed through verified) that don't have it yet.
    def _verified_event_ts(enr):
        ev = (
            StageEvent.objects.filter(enrollment_id=enr.pk, to_stage="verified")
            .order_by("entered_at")
            .first()
        )
        return ev.entered_at if ev else None

    for enr in EnrollmentVerification.objects.filter(
        stage__in=_VERIFIED_PLUS, verified_at__isnull=True
    ).iterator():
        enr.verified_at = _verified_event_ts(enr) or enr.stage_at or enr.opened_at
        enr.save(update_fields=["verified_at"])

    for enr in EnrollmentVerification.objects.filter(
        stage="on_hold", verified_at__isnull=True
    ).iterator():
        ts = _verified_event_ts(enr)
        if ts is not None:
            enr.verified_at = ts
            enr.save(update_fields=["verified_at"])

    # 3. Recompute clients still stored at a removed ClientStage. Uses the live
    #    derivation so the result reflects the new model (steps 1-2 are already
    #    applied). Scoped to the removed values only, to avoid disturbing the
    #    broader stored-stage data.
    try:
        from api.models import Client
        from api.services.lifecycle import recompute_client_stage
    except Exception:  # pragma: no cover - defensive; never block the migration
        return
    affected = Client.objects.filter(
        lifecycle_stage__in=["waiting_authorization", "authorized"]
    )
    for client in affected.iterator():
        try:
            recompute_client_stage(client)
        except Exception:  # pragma: no cover - isolate one bad client
            continue


def backward(apps, schema_editor):
    # One-way collapse: the removed stages cannot be reconstructed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0102_enrollmentverification_verified_at_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
