# Re-grain the EnrollmentAnalytics read model from per-ENROLLMENT to per-MEMBER
# (one row per Client) so EVERY member is represented -- including those with no
# enrollment / no internal-service case (Company Status = No Case Created). The
# read model is fully rebuildable, so we DROP + RECREATE the table rather than
# attempt an in-place primary-key change. Run rebuild_enrollment_analytics after.

import django.contrib.postgres.fields
import django.db.models.deletion
from django.contrib.postgres.indexes import GinIndex
from django.db import migrations, models


def _char(**kw):
    kw.setdefault("blank", True)
    return models.CharField(**kw)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0224_remove_enrollmentanalytics_has_been_delivered_and_more"),
    ]

    operations = [
        migrations.DeleteModel(name="EnrollmentAnalytics"),
        migrations.CreateModel(
            name="EnrollmentAnalytics",
            fields=[
                ("client", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                    related_name="analytics", serialize=False, to="api.client",
                )),
                ("enrollment_id", models.BigIntegerField(null=True, blank=True, db_index=True)),
                ("household_id", models.UUIDField(null=True, blank=True, db_index=True)),
                ("case_id", models.UUIDField(null=True, blank=True)),
                ("is_primary", models.BooleanField(default=False)),
                ("stage", _char(max_length=25, db_index=True)),
                ("first_name", _char(max_length=255)),
                ("last_name", _char(max_length=255)),
                ("medicaid_id", _char(max_length=64)),
                ("dob", models.DateField(null=True, blank=True, db_index=True)),
                ("member_created_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("care_coordinator", _char(max_length=255, db_index=True)),
                ("primary_care_coordinator", _char(max_length=255)),
                ("cadence", _char(max_length=40, db_index=True)),
                ("kitchen_id", models.UUIDField(null=True, blank=True, db_index=True)),
                ("kitchen_name", _char(max_length=255)),
                ("menu_type", _char(max_length=120, db_index=True)),
                ("current_delivery_status", _char(max_length=30, db_index=True)),
                ("last_po_delivery_status", _char(max_length=30)),
                ("last_delivered_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("in_any_po", models.BooleanField(default=False, db_index=True)),
                ("insurance_status", _char(max_length=20, db_index=True)),
                ("insurance_expires_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("social_status", _char(max_length=20, db_index=True)),
                ("social_expires_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("attestation_status", _char(max_length=20, db_index=True)),
                ("attestation_requested_at", models.DateTimeField(null=True, blank=True)),
                ("attestation_completed_at", models.DateTimeField(null=True, blank=True)),
                ("has_screening", models.BooleanField(default=False, db_index=True)),
                ("screening_at", models.DateTimeField(null=True, blank=True)),
                ("has_eligibility_assessment", models.BooleanField(default=False, db_index=True)),
                ("eligibility_assessment_at", models.DateTimeField(null=True, blank=True)),
                ("verified_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("verified_by_name", _char(max_length=255)),
                ("case_type", _char(max_length=20, db_index=True)),
                ("case_status", _char(max_length=25, db_index=True)),
                ("auth_status", _char(max_length=20, db_index=True)),
                ("case_opened_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("program_name", _char(max_length=255, db_index=True)),
                ("eligibility", _char(max_length=20, db_index=True)),
                ("verification_state", _char(max_length=40, db_index=True)),
                ("program_status", _char(max_length=40, db_index=True)),
                ("company_status", _char(max_length=20, db_index=True)),
                ("nutritionist_status", _char(max_length=20, db_index=True)),
                ("delivery_company", _char(max_length=255, db_index=True)),
                ("lead_source", _char(max_length=120, db_index=True)),
                ("team", _char(max_length=120, db_index=True)),
                ("service_type", _char(max_length=20, db_index=True)),
                ("program_type", _char(max_length=20, db_index=True)),
                ("out_of_orbit", models.BooleanField(default=False, db_index=True)),
                ("out_of_range", models.BooleanField(default=False, db_index=True)),
                ("paused", models.BooleanField(default=False, db_index=True)),
                ("pause_type", _char(max_length=20)),
                ("verified_by_id_str", _char(max_length=64, db_index=True)),
                ("requested_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("case_closed_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("allergies", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=64), blank=True, default=list, size=None)),
                ("medical_conditions", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=128), blank=True, default=list, size=None)),
                ("medications", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=128), blank=True, default=list, size=None)),
                ("eligible_services", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=64), blank=True, default=list, size=None)),
                ("tags", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=64), blank=True, default=list, size=None)),
                ("ticket_types", django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=64), blank=True, default=list, size=None)),
                ("refreshed_at", models.DateTimeField(auto_now=True, db_index=True)),
            ],
            options={
                "indexes": [
                    GinIndex(fields=["allergies"], name="ea_allergies_gin"),
                    GinIndex(fields=["medical_conditions"], name="ea_conditions_gin"),
                    GinIndex(fields=["medications"], name="ea_medications_gin"),
                    GinIndex(fields=["eligible_services"], name="ea_elig_services_gin"),
                    GinIndex(fields=["tags"], name="ea_tags_gin"),
                    GinIndex(fields=["ticket_types"], name="ea_ticket_types_gin"),
                ],
            },
        ),
    ]
