"""Convert legacy member ``menu_type`` CODES to the catalog MenuType NAMES now
stored on the field (MemberDietaryProfile / MemberDeliverySchedule /
OrderSchedule). Rows already holding a name (or empty) are left untouched.
"""
from django.db import migrations

_CODE_TO_NAME = {
    "standard": "Standard",
    "fish_free": "Fish Free",
    "vegetarian": "Vegetarian",
    "dairy_free": "Dairy Free",
}
_NAME_TO_CODE = {v: k for k, v in _CODE_TO_NAME.items()}

_MODELS = ("MemberDietaryProfile", "MemberDeliverySchedule", "OrderSchedule")


def _remap(apps, mapping):
    for model_name in _MODELS:
        Model = apps.get_model("api", model_name)
        for code, value in mapping.items():
            Model.objects.filter(menu_type=code).update(menu_type=value)


def forward(apps, schema_editor):
    _remap(apps, _CODE_TO_NAME)


def backward(apps, schema_editor):
    _remap(apps, _NAME_TO_CODE)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0093_deliveryorder_kitchen_food_notes_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
