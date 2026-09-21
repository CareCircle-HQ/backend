"""Decode each programme's borough from its name.

A programme name is ``Family - Item - Borough``, split on space-hyphen-space.
Only names that yield a KNOWN borough are filled: the table also holds food and
navigation programmes whose third part is not a place, and guessing would put
"(Household) Pregnant / Postpartum" in a borough column.

The known set comes from ServiceZipCode, which is the same list the address check
uses -- so the two cannot disagree about what a borough is.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    ServiceZipCode = apps.get_model("api", "ServiceZipCode")

    known = {
        b.strip() for b in
        ServiceZipCode.objects.values_list("borough", flat=True) if b and b.strip()
    }
    # Fall back to the three the housing programmes use, in case the ZIP table is
    # empty on a fresh database.
    known |= {"Brooklyn", "Manhattan", "Queens"}

    updated = 0
    for program in ActiveProgram.objects.all():
        parts = [p.strip() for p in (program.program_name or "").split(" - ")]
        candidate = parts[-1] if len(parts) >= 2 else ""
        borough = candidate if candidate in known else ""
        if program.borough != borough:
            program.borough = borough
            program.save(update_fields=["borough"])
            updated += 1
    print(f"  borough set on {updated} programme(s)")


def clear(apps, schema_editor):
    apps.get_model("api", "ActiveProgram").objects.update(borough="")


class Migration(migrations.Migration):
    dependencies = [("api", "0281_activeprogram_borough")]
    operations = [migrations.RunPython(backfill, clear)]
