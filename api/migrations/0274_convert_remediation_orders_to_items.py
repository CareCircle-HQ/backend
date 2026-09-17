"""Convert per-case remediation ORDERS into DispatchItems.

The model changed: a housing case used to become its own remediation order, one
per case. It now yields a DispatchItem -- "a grab bar in Brooklyn" -- and a work
order is a BATCH an agent assembles from approved items.

So every existing remediation order that carries a ``case`` is really an item that
was auto-created, never a deliberate dispatch decision. This migration turns each
into the item it should have been.

PRODUCTION HAS NONE of these (the dispatch tables ship in this same release), so
this is for development databases where the old flow was exercised. It is written
defensively anyway, because "there is nothing to convert" is a claim that ages
badly.

An order is only DELETED when nothing of value hangs off it -- no submissions,
findings, proofs, documents or Unite Us uploads. If any of those exist the order is
KEPT as a real work order and the new item is linked to it, because evidence
someone collected must never be dropped to tidy up a schema change. The two paths
are counted separately in the migration output.
"""
from django.db import migrations


def _parse(program_name):
    """Inlined rather than imported from api.services.housing.

    A migration must keep working against the code as it was when written; importing
    live helpers means a later refactor silently changes what this did.
    """
    parts = [p.strip() for p in (program_name or "").split(" - ")]
    if len(parts) != 3 or not all(parts):
        return "", "", ""
    return parts[0], parts[1], parts[2]


def forwards(apps, schema_editor):
    DispatchOrder = apps.get_model("api", "DispatchOrder")
    DispatchItem = apps.get_model("api", "DispatchItem")

    converted = kept = 0
    orders = (
        DispatchOrder.objects
        .filter(kind="remediation")
        .exclude(case__isnull=True)
        .select_related("case", "parent")
    )
    for order in orders:
        if order.parent_id is None:
            # An orphan work order with no assessment has nowhere to hang an item.
            # Left alone rather than guessed at.
            continue
        if DispatchItem.objects.filter(case_id=order.case_id).exists():
            continue

        _family, item, location = _parse(getattr(order.case, "program_name", ""))
        has_evidence = (
            order.submissions.exists()
            or order.findings.exists()
            or order.proofs.exists()
            or order.documents.exists()
            or order.uniteus_uploads.exists()
        )
        DispatchItem.objects.create(
            assessment_id=order.parent_id,
            case_id=order.case_id,
            # Linked to the surviving order when there is evidence; otherwise the
            # item starts unassigned, waiting for an agent to batch it.
            dispatch_order_id=order.pk if has_evidence else None,
            item=item,
            location=location,
            program_name=getattr(order.case, "program_name", "") or "",
        )
        if has_evidence:
            kept += 1
        else:
            order.delete()
            converted += 1

    if converted or kept:
        print(
            f"\n  dispatch: {converted} auto-created order(s) became items, "
            f"{kept} kept as work orders (evidence attached)",
        )


def backwards(apps, schema_editor):
    """Items are dropped; the orders they replaced are NOT recreated.

    Recreating them would have to invent statuses, and an unpicked item is not the
    same thing as a dispatched order. Reversing this migration is a development
    convenience, not a restore.
    """
    apps.get_model("api", "DispatchItem").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0273_dispatch_items"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
