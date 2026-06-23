from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Reframe per-member verification as a per-member dietary profile.

    The household (EnrollmentVerification) is the unit of verification, so the
    per-member row no longer carries its own verification outcome. We:
      * rename MemberVerification -> MemberDietaryProfile (preserves the table
        and all referencing FKs / data),
      * drop the per-member ``status`` and ``denied_reason`` fields,
      * rename the delivery-schedule FK member_verification -> member_profile,
      * make a member's delivery plan unique per program (was per product_type).
    """

    dependencies = [
        ("api", "0078_seed_cadence_rules"),
    ]

    operations = [
        # 1) Rename the model. This renames the table and repoints every FK that
        #    targets it (MemberDeliverySchedule.member_verification,
        #    OrderSchedule.member) in both the migration state and the DB.
        migrations.RenameModel(
            old_name="MemberVerification",
            new_name="MemberDietaryProfile",
        ),
        # 2) Rename the delivery-schedule FK column to match the new concept.
        migrations.RenameField(
            model_name="memberdeliveryschedule",
            old_name="member_verification",
            new_name="member_profile",
        ),
        # 3) Update related_names on the profile's FKs (Python/state-only; the
        #    accessor becomes ``enrollment.member_profiles`` / ``client.member_profiles``).
        migrations.AlterField(
            model_name="memberdietaryprofile",
            name="enrollment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="member_profiles",
                to="api.enrollmentverification",
            ),
        ),
        migrations.AlterField(
            model_name="memberdietaryprofile",
            name="client",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="member_profiles",
                to="api.client",
            ),
        ),
        # 4) Drop the per-member verification outcome — the household is the unit
        #    of verification now.
        migrations.RemoveField(model_name="memberdietaryprofile", name="status"),
        migrations.RemoveField(model_name="memberdietaryprofile", name="denied_reason"),
        # 5) Rename the unique constraint to match the new model name.
        migrations.RemoveConstraint(
            model_name="memberdietaryprofile",
            name="uniq_member_verification_per_enrollment_client",
        ),
        migrations.AddConstraint(
            model_name="memberdietaryprofile",
            constraint=models.UniqueConstraint(
                fields=("enrollment", "client"),
                name="uniq_member_dietary_profile_per_enrollment_client",
            ),
        ),
        # 6) One delivery plan per member per PROGRAM (was per product_type).
        migrations.RemoveConstraint(
            model_name="memberdeliveryschedule",
            name="uniq_member_delivery_schedule",
        ),
        migrations.AddConstraint(
            model_name="memberdeliveryschedule",
            constraint=models.UniqueConstraint(
                fields=("enrollment", "household_member", "program"),
                name="uniq_member_delivery_schedule",
            ),
        ),
    ]
