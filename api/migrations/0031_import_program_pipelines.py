# Import Program Name -> GHL pipeline mappings from CSV

from django.db import migrations
import csv
import os


def _norm(key):
    return (key or "").strip().lower().replace(" ", "_")


def import_program_pipelines(apps, schema_editor):
    ProgramPipeline = apps.get_model('api', 'ProgramPipeline')

    csv_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'tmp', 'program_name_pipelines.csv'
    )

    if not os.path.exists(csv_path):
        print(f"CSV file not found: {csv_path} - skipping program pipeline import")
        return

    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                r = {_norm(k): (v or "").strip() for k, v in row.items()}

                program_name = r.get('program_name', '')
                pipeline_id = r.get('pipeline_id', '')

                # Skip blank/padding rows and rows without a pipeline id.
                if not program_name or not pipeline_id:
                    continue

                ProgramPipeline.objects.update_or_create(
                    program_name=program_name,
                    defaults={
                        'main_category': r.get('main_category', ''),
                        'case_category': r.get('case_category', ''),
                        'services_category': r.get('services_category', ''),
                        'pipeline_name': r.get('pipeline_name', ''),
                        'pipeline_id': pipeline_id,
                    },
                )
                count += 1
            print(f"Imported {count} program pipeline mappings")
    except Exception as e:
        # Never fail the migration just because the CSV is malformed.
        print(f"Program pipeline CSV import skipped due to error: {e}")


def reverse_import(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0030_programpipeline'),
    ]

    operations = [
        migrations.RunPython(import_program_pipelines, reverse_import),
    ]
