"""One-off: convert stored ``Client.lead_source`` VALUES from CallTools ids to
their queue/campaign NAMES.

Background: the extension used to save the CallTools queue *id* (e.g. "5975")
into ``Client.lead_source``. It now saves the queue/campaign *name* (free text,
e.g. "Hyphen Met"). All filters/reports match the stored value case-insensitively
(``lead_source__iexact``), so a legacy id-value never matches a name option.

This command resolves every stored value that is a known CallTools id to its
name (using the same queue+campaign source the ext/reports use) and rewrites it.
Values that are already names (or ids we can't resolve, e.g. a deleted queue)
are left untouched and reported.

is_williamsburg is re-derived from the resulting name so it stays consistent
with the serializer's rule (name == "Williamsburg").

Idempotent. Dry-run unless ``--apply``.

Usage:
    python manage.py migrate_lead_source_to_name              # dry run
    python manage.py migrate_lead_source_to_name --apply      # commit
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Client


def _id_to_name_map():
    """CallTools queue/campaign id -> name, from the same source the ext and the
    reports export use. Empty (safe no-op) if CallTools is disabled/unreachable."""
    mapping = {}
    try:
        from api.integrations.calltools import campaigns as ct_campaigns
        from api.integrations.calltools import config as ct_config
        from api.integrations.calltools import queues as ct_queues

        if not ct_config.is_enabled():
            return mapping
        ct_options = []
        try:
            ct_options += ct_queues.list_queue_options()
        except Exception:
            pass
        try:
            ct_options += ct_campaigns.list_campaign_options()
        except Exception:
            pass
        for q in ct_options:
            qid = str(q.get("id") or "").strip()
            name = (q.get("name") or "").strip()
            if qid and name:
                mapping.setdefault(qid, name)
    except Exception:
        pass
    return mapping


class Command(BaseCommand):
    help = (
        "Convert stored Client.lead_source values from CallTools ids to their "
        "queue/campaign names. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes.")

    def handle(self, *args, **options):
        apply = options["apply"]
        head = self.style.MIGRATE_HEADING

        id_to_name = _id_to_name_map()
        if not id_to_name:
            self.stdout.write(self.style.ERROR(
                "No CallTools id->name map available (CallTools disabled or "
                "unreachable). Cannot resolve ids to names -- aborting."
            ))
            return

        # Case-insensitive lookup: some stored names may already be present; we
        # only rewrite values that match a KNOWN id key exactly.
        clients = (
            Client.objects.exclude(lead_source="")
            .only("client_id", "lead_source", "is_williamsburg")
        )

        buckets = Counter()
        changes = []          # (client_id, old, new)
        unresolved = Counter()  # raw value -> count (looked like an id but no map hit)

        for c in clients:
            raw = (c.lead_source or "").strip()
            if not raw:
                buckets["blank"] += 1
                continue
            name = id_to_name.get(raw)
            if name is None:
                # Already a name (matches a known name), or an unknown/legacy value.
                if raw in id_to_name.values():
                    buckets["already_name"] += 1
                elif raw.isdigit():
                    # Looks like an id but not in the current map (deleted queue?).
                    buckets["unresolved_id"] += 1
                    unresolved[raw] += 1
                else:
                    buckets["free_text"] += 1
                continue
            if name == raw:
                buckets["already_name"] += 1
                continue
            changes.append((str(c.client_id), raw, name))
            buckets["to_convert"] += 1

        # -- apply -------------------------------------------------------------
        if apply and changes:
            with transaction.atomic():
                for cid, old, new in changes:
                    obj = Client.objects.get(client_id=cid)
                    obj.lead_source = new
                    fields = ["lead_source"]
                    want_wburg = new.strip().lower() == "williamsburg"
                    if obj.is_williamsburg != want_wburg:
                        obj.is_williamsburg = want_wburg
                        fields.append("is_williamsburg")
                    obj.save(update_fields=fields)

        # -- report ------------------------------------------------------------
        self.stdout.write(head("\n=== Migrate lead_source id -> name ==="))
        self.stdout.write(f"  {'CallTools id->name entries':<32}: {len(id_to_name)}")
        self.stdout.write(f"  {'Clients with a lead_source':<32}: {clients.count()}")
        self.stdout.write(f"  {'Will convert (id -> name)':<32}: {buckets['to_convert']}")
        self.stdout.write(f"  {'Already a name':<32}: {buckets['already_name']}")
        self.stdout.write(f"  {'Other free text (left as-is)':<32}: {buckets['free_text']}")
        self.stdout.write(f"  {'Unresolved ids (left as-is)':<32}: {buckets['unresolved_id']}")

        if changes:
            self.stdout.write(head(f"\nConversions ({len(changes)}):"))
            for cid, old, new in changes[:200]:
                self.stdout.write(f"  {cid}: {old!r} -> {new!r}")
            if len(changes) > 200:
                self.stdout.write(f"  ... and {len(changes) - 200} more")

        if unresolved:
            self.stdout.write(self.style.WARNING(
                f"\nUnresolved id-like values ({len(unresolved)} distinct) -- these "
                "look like ids but aren't in the current CallTools list (deleted "
                "queue/campaign?). Left untouched; resolve manually if needed:"
            ))
            for val, n in unresolved.most_common(50):
                self.stdout.write(f"  {val!r}: {n} client(s)")

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"\nAPPLIED (committed): {len(changes)} client(s) updated."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN: nothing written. Re-run with --apply to commit."
            ))
