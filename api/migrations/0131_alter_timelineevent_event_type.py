# Generated manually: add HOUSEHOLD_MEMBER_REMOVED to TimelineEventType.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0130_memberwarning'),
    ]

    operations = [
        migrations.AlterField(
            model_name='timelineevent',
            name='event_type',
            field=models.CharField(choices=[('consent_granted', 'Consent Granted'), ('insurance', 'Insurance'), ('social_care_coverage', 'Social Care Coverage'), ('screening', 'Screening'), ('assessment', 'Assessment'), ('case_opened', 'Case'), ('case_status_changed', 'Case Status Changed'), ('case_auth_changed', 'Case Authorization Changed'), ('pending_validation', 'Pending Validation'), ('validated', 'Validated'), ('verification_requested', 'Verification Requested'), ('verification_completed', 'Verification Completed'), ('waiting_authorization', 'Waiting Authorization'), ('authorized', 'Authorized'), ('denied', 'Denied'), ('kitchen_assigned', 'Kitchen Assigned'), ('service_activated', 'Service Activated'), ('service_on_hold', 'Service On Hold'), ('service_resumed', 'Service Resumed'), ('service_completed', 'Service Completed'), ('service_closed', 'Service Closed'), ('service_cancelled', 'Service Cancelled'), ('enrolled', 'Enrolled'), ('ticket_created', 'New Ticket Created'), ('delivery_address_changed', 'Delivery Address Changed'), ('out_of_orbit', 'Out of Orbit'), ('out_of_range', 'Out of Range'), ('member_reactivated', 'Member Reactivated'), ('member_paused', 'Member Paused'), ('member_unpaused', 'Member Unpaused'), ('household_member_added', 'Household Member Added'), ('household_member_removed', 'Household Member Removed'), ('product_type_changed', 'Product Type Changed'), ('verification', 'Verification'), ('service', 'Service'), ('verification_disregarded', 'Verification Disregarded')], db_index=True, max_length=30),
        ),
    ]
