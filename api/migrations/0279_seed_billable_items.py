"""Seed the housing price list from Simplified_Billable_Items_Pricing.

26 physical items plus the dwelling assessment. Every physical item matches an
intervention option in api/services/assessment_forms.py exactly, in both
directions, so ``option_code`` is a real join rather than a label match.

Prices are what we RECOMMEND a vendor charge us. The billed price is derived
(vendor price + the adjustable admin fee) and deliberately not stored -- see the
BillableItem docstring.

Idempotent via update_or_create on (item, billing_category), so a re-run corrects
prices rather than duplicating rows.
"""
from decimal import Decimal

from django.db import migrations

# main category, item, option_code, billing category, modifiers, vendor price
ROWS = [
    ("Bathroom", "Shower chair", "shower_chair", "Bathroom Facilities", "U1, U4", "456.75"),
    ("Bathroom", "Bath bench", "bath_bench", "Bathroom Facilities", "U1, U4", "509.25"),
    ("Bathroom", "Raised toilet seat", "raised_toilet_seat", "Bathroom Facilities", "U1, U4", "509.25"),
    ("Bathroom", "Non-skid bath mat", "non_skid_bath_mat", "Non-skid Surfaces", "U3, U4", "325.50"),
    ("Bathroom", "Non-slip adhesive strips", "non_slip_adhesive_strips", "Non-skid Surfaces", "U3, U4", "404.25"),
    ("Bathroom", "Non-slip tape", "non_slip_tape", "Non-skid Surfaces", "U3, U4", "404.25"),
    ("Bathroom", "Grab bar at toilet", "grab_bar_toilet", "Grab Bars", "U5, UC", "498.75"),
    ("Bathroom", "Grab bar at tub", "grab_bar_tub", "Grab Bars", "U5, UC", "498.75"),
    ("Bathroom", "Grab bar at shower", "grab_bar_shower", "Grab Bars", "U5, UC", "498.75"),
    ("Bathroom", "Floor-to-ceiling safety pole", "safety_pole", "Grab Bars", "U5, UC", "693.00"),

    ("Doors, Handles & Access", "Lever door handle", "lever_door_handle", "Doors & Cabinet Handles", "U4, UB", "430.50"),
    ("Doors, Handles & Access", "D-ring cabinet pull", "d_ring_cabinet_pull", "Doors & Cabinet Handles", "U4, UB", "262.50"),
    ("Doors, Handles & Access", "Loop cabinet handle", "loop_cabinet_handle", "Doors & Cabinet Handles", "U4, UB", "262.50"),

    ("Mobility & Access", "Modular/portable ramp", "modular_portable_ramp", "Accessibility Ramps", "U3, UC", "876.75"),
    ("Mobility & Access", "Threshold ramp", "threshold_ramp", "Accessibility Ramps", "U3, UC", "666.75"),
    ("Mobility & Access", "Interior staircase handrail", "staircase_handrail", "Handrails", "U4, UC", "666.75"),
    ("Mobility & Access", "Hallway handrail", "hallway_handrail", "Handrails", "U4, UC", "666.75"),
    ("Mobility & Access", "Threshold reducer", "threshold_reducer", "Pathways", "U2, U8", "456.75"),

    ("Air Quality", "HEPA air purifier", "hepa_purifier", "Air Filtration Devices", "UA, UC", "693.00"),
    ("Air Quality", "Portable air filtration unit", "portable_filtration_unit", "Air Filtration Devices", "UA, UC", "693.00"),
    ("Air Quality", "Dehumidifier (portable)", "dehumidifier_portable", "De-humidifier", "U4, U7", "693.00"),
    ("Air Quality", "Portable humidifier", "portable_humidifier", "Humidifier", "U5, U4", "561.75"),
    ("Air Quality", "Cool mist humidifier", "cool_mist_humidifier", "Humidifier", "U5, U4", "666.75"),

    ("Temperature Control", "Window air conditioner", "window_ac", "Air Conditioner", "U8, UC", "1323.00"),
    ("Temperature Control", "Portable air conditioner", "portable_ac", "Air Conditioner", "U8, UC", "903.00"),
    ("Temperature Control", "Portable space heater", "portable_space_heater", "Heater", "U9, UC", "509.25"),

    # Not an installable item, so no option_code: the assessment is the visit
    # itself, priced as its own billable line.
    ("Assessment", "Dwelling assessment", "", "Dwelling Assessment & SOW Development", "UA, U8", "750.00"),
]

HCPCS = "S5165"


def seed(apps, schema_editor):
    BillableItem = apps.get_model("api", "BillableItem")
    BillingSettings = apps.get_model("api", "BillingSettings")

    for order, (main, item, code, category, modifiers, price) in enumerate(ROWS):
        BillableItem.objects.update_or_create(
            item=item, billing_category=category,
            defaults={
                "option_code": code,
                "main_category": main,
                "hcpcs_code": HCPCS,
                "modifiers": modifiers,
                "vendor_price": Decimal(price),
                "sort_order": order,
                "is_active": True,
            },
        )
    # The 10% the sheet applies. Adjustable from the CRM afterwards.
    BillingSettings.objects.update_or_create(
        singleton_id=1, defaults={"admin_fee_percent": Decimal("10.00")},
    )


def unseed(apps, schema_editor):
    BillableItem = apps.get_model("api", "BillableItem")
    BillableItem.objects.filter(
        item__in=[r[1] for r in ROWS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("api", "0278_billable_items")]
    operations = [migrations.RunPython(seed, unseed)]
