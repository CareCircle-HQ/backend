"""Update the PHS service-area ZIP whitelist to the approved 8/18/26 list.

Reconciles ``ServiceZipCode`` to EXACTLY the approved list (Manhattan / Brooklyn /
Queens): every listed ZIP is upserted ACTIVE with its borough, and any ZIP NOT on
the list is REMOVED. Data lives here so it ships to prod. Idempotent.
"""

from django.db import migrations

# Approved whitelist -- 8/18/26. (zip, borough). 171 ZIPs.
WHITELIST = [
    # --- Manhattan (65) ---
    ('10001', 'Manhattan'), ('10002', 'Manhattan'), ('10003', 'Manhattan'), ('10004', 'Manhattan'),
    ('10005', 'Manhattan'), ('10006', 'Manhattan'), ('10007', 'Manhattan'), ('10009', 'Manhattan'),
    ('10010', 'Manhattan'), ('10011', 'Manhattan'), ('10012', 'Manhattan'), ('10013', 'Manhattan'),
    ('10014', 'Manhattan'), ('10016', 'Manhattan'), ('10017', 'Manhattan'), ('10018', 'Manhattan'),
    ('10019', 'Manhattan'), ('10020', 'Manhattan'), ('10021', 'Manhattan'), ('10022', 'Manhattan'),
    ('10023', 'Manhattan'), ('10024', 'Manhattan'), ('10025', 'Manhattan'), ('10026', 'Manhattan'),
    ('10027', 'Manhattan'), ('10028', 'Manhattan'), ('10029', 'Manhattan'), ('10030', 'Manhattan'),
    ('10031', 'Manhattan'), ('10032', 'Manhattan'), ('10033', 'Manhattan'), ('10034', 'Manhattan'),
    ('10035', 'Manhattan'), ('10036', 'Manhattan'), ('10037', 'Manhattan'), ('10038', 'Manhattan'),
    ('10039', 'Manhattan'), ('10040', 'Manhattan'), ('10044', 'Manhattan'), ('10065', 'Manhattan'),
    ('10069', 'Manhattan'), ('10075', 'Manhattan'), ('10128', 'Manhattan'), ('10162', 'Manhattan'),
    ('10165', 'Manhattan'), ('10167', 'Manhattan'), ('10168', 'Manhattan'), ('10169', 'Manhattan'),
    ('10170', 'Manhattan'), ('10171', 'Manhattan'), ('10172', 'Manhattan'), ('10173', 'Manhattan'),
    ('10174', 'Manhattan'), ('10175', 'Manhattan'), ('10176', 'Manhattan'), ('10177', 'Manhattan'),
    ('10178', 'Manhattan'), ('10199', 'Manhattan'), ('10270', 'Manhattan'), ('10271', 'Manhattan'),
    ('10278', 'Manhattan'), ('10279', 'Manhattan'), ('10280', 'Manhattan'), ('10281', 'Manhattan'),
    ('10282', 'Manhattan'),
    # --- Brooklyn (39) ---
    ('11201', 'Brooklyn'), ('11202', 'Brooklyn'), ('11203', 'Brooklyn'), ('11204', 'Brooklyn'),
    ('11205', 'Brooklyn'), ('11206', 'Brooklyn'), ('11207', 'Brooklyn'), ('11208', 'Brooklyn'),
    ('11210', 'Brooklyn'), ('11211', 'Brooklyn'), ('11212', 'Brooklyn'), ('11213', 'Brooklyn'),
    ('11214', 'Brooklyn'), ('11215', 'Brooklyn'), ('11216', 'Brooklyn'), ('11217', 'Brooklyn'),
    ('11218', 'Brooklyn'), ('11221', 'Brooklyn'), ('11222', 'Brooklyn'), ('11223', 'Brooklyn'),
    ('11224', 'Brooklyn'), ('11225', 'Brooklyn'), ('11226', 'Brooklyn'), ('11229', 'Brooklyn'),
    ('11230', 'Brooklyn'), ('11231', 'Brooklyn'), ('11232', 'Brooklyn'), ('11233', 'Brooklyn'),
    ('11234', 'Brooklyn'), ('11235', 'Brooklyn'), ('11236', 'Brooklyn'), ('11237', 'Brooklyn'),
    ('11238', 'Brooklyn'), ('11239', 'Brooklyn'), ('11241', 'Brooklyn'), ('11242', 'Brooklyn'),
    ('11249', 'Brooklyn'), ('11252', 'Brooklyn'), ('11256', 'Brooklyn'),
    # --- Queens (67) ---  (11001 is on the Nassau border but approved)
    ('11001', 'Queens'), ('11004', 'Queens'), ('11005', 'Queens'), ('11101', 'Queens'),
    ('11102', 'Queens'), ('11103', 'Queens'), ('11104', 'Queens'), ('11105', 'Queens'),
    ('11106', 'Queens'), ('11109', 'Queens'), ('11351', 'Queens'), ('11354', 'Queens'),
    ('11356', 'Queens'), ('11357', 'Queens'), ('11358', 'Queens'), ('11359', 'Queens'),
    ('11360', 'Queens'), ('11361', 'Queens'), ('11362', 'Queens'), ('11363', 'Queens'),
    ('11364', 'Queens'), ('11365', 'Queens'), ('11366', 'Queens'), ('11367', 'Queens'),
    ('11369', 'Queens'), ('11370', 'Queens'), ('11371', 'Queens'), ('11372', 'Queens'),
    ('11374', 'Queens'), ('11375', 'Queens'), ('11378', 'Queens'), ('11379', 'Queens'),
    ('11385', 'Queens'), ('11411', 'Queens'), ('11412', 'Queens'), ('11413', 'Queens'),
    ('11414', 'Queens'), ('11415', 'Queens'), ('11416', 'Queens'), ('11417', 'Queens'),
    ('11418', 'Queens'), ('11419', 'Queens'), ('11420', 'Queens'), ('11421', 'Queens'),
    ('11422', 'Queens'), ('11423', 'Queens'), ('11424', 'Queens'), ('11425', 'Queens'),
    ('11426', 'Queens'), ('11427', 'Queens'), ('11428', 'Queens'), ('11429', 'Queens'),
    ('11430', 'Queens'), ('11432', 'Queens'), ('11433', 'Queens'), ('11434', 'Queens'),
    ('11435', 'Queens'), ('11436', 'Queens'), ('11439', 'Queens'), ('11451', 'Queens'),
    ('11690', 'Queens'), ('11691', 'Queens'), ('11692', 'Queens'), ('11693', 'Queens'),
    ('11694', 'Queens'), ('11695', 'Queens'), ('11697', 'Queens'),
]


def apply_whitelist(apps, schema_editor):
    ServiceZipCode = apps.get_model("api", "ServiceZipCode")
    keep = {z for z, _ in WHITELIST}
    # Remove any ZIP not on the approved list.
    ServiceZipCode.objects.exclude(zip__in=keep).delete()
    # Upsert every approved ZIP: active, with its borough.
    for z, b in WHITELIST:
        ServiceZipCode.objects.update_or_create(
            zip=z, defaults={"borough": b, "is_active": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0244_client_hyros_enrolled_pushed_at_and_more'),
    ]

    operations = [
        # Not reversibly restorable (the prior arbitrary set is gone); no-op reverse.
        migrations.RunPython(apply_whitelist, migrations.RunPython.noop),
    ]
