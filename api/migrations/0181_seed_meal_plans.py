from django.db import migrations

SEED_PLANS = [
    "Weight management",
    "Diabetes / Pre-diabetes",
    "Cardiac / Hypertension",
    "Prenatal / postpartum",
    "General/standard",
]


def seed(apps, schema_editor):
    MealPlan = apps.get_model("api", "MealPlan")
    for name in SEED_PLANS:
        MealPlan.objects.get_or_create(name=name)


def unseed(apps, schema_editor):
    MealPlan = apps.get_model("api", "MealPlan")
    MealPlan.objects.filter(name__in=SEED_PLANS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0180_mealplan"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
