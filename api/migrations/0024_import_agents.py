# Import agents from CSV

from django.db import migrations
import csv
import os


def _norm(key):
    return (key or "").strip().lower().replace(" ", "_")


def import_agents(apps, schema_editor):
    Agent = apps.get_model('api', 'Agent')

    csv_path = os.path.join(os.path.dirname(__file__), '..', '..', 'tmp', 'Agent_Database_.csv')

    if not os.path.exists(csv_path):
        print(f"CSV file not found: {csv_path} - skipping agent import")
        return

    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                # Normalize keys so header variations don't break the import
                r = {_norm(k): (v or "").strip() for k, v in row.items()}

                name = r.get('name', '')
                agent_code = r.get('agent_code') or r.get('code') or r.get('agent')
                group = r.get('group', 'Screeners') or 'Screeners'
                status = r.get('status', 'Active') or 'Active'
                cbo = r.get('cbo', 'Met Council') or 'Met Council'

                if name and agent_code:
                    Agent.objects.get_or_create(
                        agent_code=agent_code,
                        defaults={
                            'name': name,
                            'group': group if group in ['Screeners', 'Verifiers', 'Management', 'CS'] else 'Screeners',
                            'status': status if status in ['Active', 'Inactive'] else 'Active',
                            'cbo': cbo,
                        }
                    )
                    count += 1
            print(f"Imported {count} agents")
    except Exception as e:
        # Never fail the migration just because the CSV is malformed.
        print(f"Agent CSV import skipped due to error: {e}")


def reverse_import(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0023_agent'),
    ]

    operations = [
        migrations.RunPython(import_agents, reverse_import),
    ]
