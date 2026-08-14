"""Reconcile Unite Us person migrations from a CSV of old->new id mappings.

Unite Us migrates some people to a NEW canonical id (GET /people/<old> -> 301);
the cases re-parent to the new id while our internal service state stays on the
old duplicate Client. This consolidates each pair onto the NEW (surviving)
client (see api.services.client_migration.merge_migrated_client).

CSV: a header row with columns ``requested`` (old id) and ``canonical`` (new id)
-- the exact shape the extension stores under chrome.storage.local["uw_idmap"].
``old``/``new`` are also accepted as column names.

Dry-run by default (prints what WOULD merge); pass --apply to commit.
"""
import csv

from django.core.management.base import BaseCommand, CommandError

from api.services.client_migration import merge_migrated_client, resolve_client


class Command(BaseCommand):
    help = (
        "Merge Unite Us migrated duplicate clients (old->new canonical id) from a "
        "CSV. Dry-run by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="CSV with requested/canonical (or old/new) columns.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually merge. Without this the command only previews.",
        )

    def _read_pairs(self, path):
        try:
            fh = open(path, newline="", encoding="utf-8-sig")
        except OSError as e:
            raise CommandError(f"Could not open {path}: {e}")
        pairs = []
        with fh:
            reader = csv.DictReader(fh)
            cols = {c.lower(): c for c in (reader.fieldnames or [])}
            old_col = cols.get("requested") or cols.get("old") or cols.get("old_id")
            new_col = cols.get("canonical") or cols.get("new") or cols.get("new_id")
            if not old_col or not new_col:
                raise CommandError(
                    "CSV must have 'requested'/'canonical' (or 'old'/'new') columns."
                )
            for row in reader:
                old_id = (row.get(old_col) or "").strip()
                new_id = (row.get(new_col) or "").strip()
                if old_id and new_id and old_id != new_id:
                    pairs.append((old_id, new_id))
        return pairs

    def handle(self, *args, **opts):
        pairs = self._read_pairs(opts["csv_path"])
        self.stdout.write(f"Read {len(pairs)} old->new mapping(s).")

        merged = skipped = 0
        for old_id, new_id in pairs:
            new_client = resolve_client(new_id)
            old_client = resolve_client(old_id)
            if new_client is None:
                self.stdout.write(f"  SKIP {old_id} -> {new_id}: canonical client not found.")
                skipped += 1
                continue
            if old_client is None or old_client.pk == new_client.pk:
                self.stdout.write(f"  SKIP {old_id} -> {new_id}: already reconciled.")
                skipped += 1
                continue

            summary = merge_migrated_client(
                old_client, new_client,
                actor_label="cmd:merge_migrated_clients",
                dry_run=not opts["apply"],
            )
            verb = "WOULD MERGE" if not opts["apply"] else "MERGED"
            self.stdout.write(
                f"  {verb} {old_id} -> {new_id}: "
                f"{summary.get('cases', 0)} case(s), "
                f"{summary.get('enrollments', 0)} enrollment(s), "
                f"{summary.get('member_profiles', 0)} member profile(s), "
                f"{summary.get('tags', 0)} tag(s)."
            )
            merged += 1

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"Dry run -- no changes. {merged} pair(s) would merge, {skipped} skipped. "
                "Re-run with --apply to commit."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Merged {merged} pair(s); skipped {skipped}."
            ))
