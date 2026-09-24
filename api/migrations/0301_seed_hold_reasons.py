"""Seed the on-hold reason catalogue.

Replaces free text: 7,379 holds across 443 distinct notes, of which 1,961 are agent
free text with 1,556 distinct reasons.

``resume_policy`` is DESCRIPTIVE -- no code acts on it yet. It records what it takes
to come off each hold so the decision lives in the catalogue rather than in a
conversation, and so the UI can tell an agent whether to wait, act, or that nothing
will clear it.

``is_system`` reasons are hidden from the agent's picker: choosing "Governing Case
Denied" by hand would assert something the case data has not said, and each has its
own remedy.
"""
from django.db import migrations

# code, label, resume_policy, resume_detail, is_system
REASONS = [
    (
        "pending_case_closure", "Pending Case Closure", "none",
        "The case is closing — nothing will lift this hold.", False,
    ),
    (
        "wrong_case_type", "Wrong Case Type Opened", "auto",
        "Resumes when the correct case is saved.", False,
    ),
    (
        "not_enhanced_member", "Not an Enhanced Member", "none",
        "The member does not qualify for Enhanced services.", False,
    ),
    (
        "governing_case_denied", "Governing Case Denied", "auto",
        "Resumes when a new governing case is saved.", True,
    ),
    (
        "governing_case_closed", "Governing Case Closed", "auto",
        "Resumes when a new case is saved.", True,
    ),
    (
        "social_coverage_invalid", "Social care coverage expired/missing", "auto",
        "Resumes when new social care coverage is Enrolled, the case is open and "
        "the authorization window is still live.", True,
    ),
    (
        "insurance_invalid", "Insurance expired/missing", "auto",
        "Resumes when new insurance is added, the case is open and the "
        "authorization window is still live.", True,
    ),
    (
        "member_requested", "Member wants to pause", "manual",
        "The member asked to stop — an agent resumes when they ask to return.",
        False,
    ),
    (
        "zip_out_of_coverage", "Delivery ZIP outside coverage", "none",
        "The address is outside the service area; correcting the ZIP is the remedy.",
        True,
    ),
    (
        "medicaid_type_not_served", "Medicaid plan type not served", "none",
        "The plan type is not one we serve.", True,
    ),
    (
        "all_members_paused", "All household members paused", "auto",
        "Resumes automatically as soon as any member is unpaused.", True,
    ),
    (
        "uncategorized", "Uncategorized", "none",
        "No category recorded.", False,
    ),
]


def seed(apps, schema_editor):
    HoldReason = apps.get_model("api", "HoldReason")
    created = updated = 0
    for order, (code, label, policy, detail, is_system) in enumerate(REASONS, 1):
        obj, was_created = HoldReason.objects.update_or_create(
            code=code,
            defaults={
                "label": label,
                "resume_policy": policy,
                "resume_detail": detail,
                "is_system": is_system,
                "sort_order": order,
                "is_active": True,
            },
        )
        created += was_created
        updated += not was_created
    print(f"  hold reasons: {created} created, {updated} updated")


def unseed(apps, schema_editor):
    # Only the seeded codes. A reason an agent added later is theirs, not ours.
    HoldReason = apps.get_model("api", "HoldReason")
    HoldReason.objects.filter(code__in=[r[0] for r in REASONS]).delete()


class Migration(migrations.Migration):
    dependencies = [("api", "0300_hold_reason")]
    operations = [migrations.RunPython(seed, unseed)]
