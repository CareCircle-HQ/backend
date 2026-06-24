"""Promote the Ticket ``type`` enum to a first-class TicketType table.

Data-preserving: the existing CharField codes are seeded into the new
``TicketType`` table and every existing Ticket (and its history rows) is
re-pointed at the matching row before the old column is dropped.
"""

import uuid

from django.db import migrations, models
import django.db.models.deletion


# Canonical codes that previously lived in the TicketType TextChoices enum.
TICKET_TYPE_SEED = [
    ("no_active_insurance", "No active insurance"),
    ("insurance_expired", "Insurance expired"),
    ("no_active_coverage", "No active social care coverage"),
    ("coverage_expired", "Social care coverage expired"),
    ("member_not_found", "Member not found"),
    ("case_closed", "Case closed"),
    ("authorization_changed", "Authorization status changed"),
    ("case_no_services", "Case with no contracted services"),
    ("new_insurance", "New insurance created (validate)"),
    ("new_coverage", "New social care coverage created"),
    ("address_out_of_area", "Address outside coverage area"),
    ("credential_expired", "Unite Us login expired (re-login)"),
]


def seed_ticket_types(apps, schema_editor):
    TicketType = apps.get_model("api", "TicketType")
    for code, label in TICKET_TYPE_SEED:
        TicketType.objects.get_or_create(code=code, defaults={"label": label})


def link_tickets(apps, schema_editor):
    """Point every Ticket / HistoricalTicket at the TicketType whose code
    matches the old string value. Unknown codes are created on the fly so no
    row is left without a type."""
    TicketType = apps.get_model("api", "TicketType")
    Ticket = apps.get_model("api", "Ticket")
    HistoricalTicket = apps.get_model("api", "HistoricalTicket")

    by_code = {t.code: t for t in TicketType.objects.all()}

    def resolve(code):
        if not code:
            return None
        tt = by_code.get(code)
        if tt is None:
            tt = TicketType.objects.create(code=code, label=code)
            by_code[code] = tt
        return tt

    for tk in Ticket.objects.all():
        tt = resolve(tk.type)
        if tt is not None:
            tk.type_tmp = tt
            tk.save(update_fields=["type_tmp"])

    for h in HistoricalTicket.objects.all():
        h.type_tmp = resolve(h.type)
        h.save(update_fields=["type_tmp"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0067_program_active_program_description_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketType",
            fields=[
                ("ticket_type_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.SlugField(max_length=40, unique=True)),
                ("label", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("default_severity", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="medium", max_length=10)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["label"],
            },
        ),
        migrations.RunPython(seed_ticket_types, migrations.RunPython.noop),
        # The old (status, type) index sits on the soon-to-be-dropped column.
        migrations.RemoveIndex(
            model_name="ticket",
            name="api_ticket_status_3855a0_idx",
        ),
        # Temporary nullable FK columns we populate before the swap.
        migrations.AddField(
            model_name="ticket",
            name="type_tmp",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="tickets", to="api.tickettype"),
        ),
        migrations.AddField(
            model_name="historicalticket",
            name="type_tmp",
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name="+", to="api.tickettype"),
        ),
        migrations.RunPython(link_tickets, migrations.RunPython.noop),
        migrations.RemoveField(model_name="ticket", name="type"),
        migrations.RemoveField(model_name="historicalticket", name="type"),
        migrations.RenameField(model_name="ticket", old_name="type_tmp", new_name="type"),
        migrations.RenameField(model_name="historicalticket", old_name="type_tmp", new_name="type"),
        migrations.AlterField(
            model_name="ticket",
            name="type",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tickets", to="api.tickettype"),
        ),
        migrations.AddIndex(
            model_name="ticket",
            index=models.Index(fields=["status", "type"], name="api_ticket_status_d4df54_idx"),
        ),
    ]
