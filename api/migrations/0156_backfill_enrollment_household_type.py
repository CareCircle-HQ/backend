from django.db import migrations


def backfill_household_type(apps, schema_editor):
    """Autofill the scope each existing enrollment was VERIFIED under onto
    ``household_type_override`` (its own persisted scope).

    Source, in order: the case the enrollment is TIED to (``enr.case`` -- the
    case the verification attached to, i.e. what it was verified under), else any
    internal-service case for the client. The governing case is never changed; a
    later divergence between this stored scope and the current governing case is
    exactly the mismatch the Household tab lets an agent reconcile.
    """
    EnrollmentVerification = apps.get_model("api", "EnrollmentVerification")
    Case = apps.get_model("api", "Case")

    qs = EnrollmentVerification.objects.filter(
        household_type_override=""
    ).only("id", "case_id", "client_id")
    for enr in qs.iterator():
        ht = ""
        if enr.case_id:
            ht = getattr(enr.case, "household_type", "") or ""
        if not ht and enr.client_id:
            c = (
                Case.objects.filter(
                    client_id=enr.client_id, case_type="internal_service"
                )
                .exclude(household_type="")
                .first()
            )
            ht = getattr(c, "household_type", "") if c is not None else ""
        if ht:
            EnrollmentVerification.objects.filter(pk=enr.pk).update(
                household_type_override=ht
            )


def noop_reverse(apps, schema_editor):
    # Non-reversible data backfill; leave values in place on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0155_enrollmentverification_household_type_override"),
    ]

    operations = [
        migrations.RunPython(backfill_household_type, noop_reverse),
    ]
