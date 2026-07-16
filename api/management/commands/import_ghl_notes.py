"""Import GoHighLevel timeline Contact Notes into our CRM as ``Note`` rows on
the matching member (primary household ``Client``).

Re-runnable / idempotent: each GHL note is upserted by its GHL note id
(``Note.source_note_id`` with ``source='ghl'``), so running it again updates
existing notes instead of duplicating them.

Mapping is deterministic — a note's contact resolves to our ``Client`` ONLY via
the external id GHL stores on the contact (Enrollment Platform Client ID =
``Client.pk``); contacts without it (e.g. phone-only leads) are skipped and
counted. See ``scan_ghl_notes`` / ``docs/ghl_notes_pull_analysis.md``.

Per note we store:
  - client            -> primary household Client (mapping.local_client_id)
  - source            -> NoteSource.GHL
  - source_note_id    -> GHL note id (idempotency key)
  - body              -> "{title}\\n\\n{bodyText}" (title + clean bodyText)
  - source_created_at -> the note's GHL dateAdded (its original date)
  - content_hash      -> sha256(body)

    # test on specific contacts (fast, no full scan)
    python manage.py import_ghl_notes --contact 9yvFFdpnS97s3djwG6iS --dry-run

    # full location-wide import (hours; run detached) -- add --dry-run first
    python manage.py import_ghl_notes --dry-run
    python manage.py import_ghl_notes

Writes only to OUR database (not gated by CRM_SYNC_DISCONNECTED, which guards
OUTBOUND writes to GHL). Needs a valid GHL_PRIVATE_TOKEN + GHL_LOCATION_ID.
"""
import hashlib
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from api.integrations.ghl import config
from api.management.commands.scan_ghl_notes import Command as ScanCommand
from api.models import Note, NoteSource


def _note_body(note):
    """Combine the GHL note title + clean bodyText into our single body field
    (our Note has no title). Returns "" when there is nothing to store."""
    title = (note.get("title") or "").strip()
    text = (note.get("bodyText") or "").strip()
    if title and text:
        return f"{title}\n\n{text}"
    return title or text


class Command(BaseCommand):
    help = "Import GHL contact notes into CRM member notes (idempotent upsert)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--contact", action="append", default=[], dest="contacts",
            help="Only import notes for these GHL contact id(s). Repeatable. "
                 "Skips the full location scan.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be imported without writing to the DB.",
        )
        parser.add_argument(
            "--max-contacts", type=int, default=0,
            help="Stop after scanning N contacts (0 = all). Full-scan mode only.",
        )
        parser.add_argument("--rate", type=float, default=8.0,
                            help="Max requests/second throttle (default 8).")
        parser.add_argument("--page-size", type=int, default=100,
                            help="Contacts per search page (GHL max 100).")

    # -- per-note upsert ----------------------------------------------------
    def _upsert_note(self, note, client_id, dry_run):
        """Upsert one GHL note as a Note on client_id. Returns 'created',
        'updated', or 'skipped' (empty body / no id)."""
        gid = note.get("id")
        body = _note_body(note)
        if not gid or not body:
            return "skipped"
        source_created = None
        da = note.get("dateAdded")
        if da:
            source_created = parse_datetime(da.replace("Z", "+00:00"))
        defaults = {
            "client_id": client_id,
            "case": None,
            "author_name": "",  # GHL notes carry a GHL userId, not our Agent
            "body": body,
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "source_created_at": source_created,
        }
        if dry_run:
            exists = Note.objects.filter(
                source=NoteSource.GHL, source_note_id=gid
            ).exists()
            return "updated" if exists else "created"
        _, created = Note.objects.update_or_create(
            source=NoteSource.GHL, source_note_id=gid, defaults=defaults,
        )
        return "created" if created else "updated"

    def _process_contact(self, gh, contact_id, contact_name, stats, dry_run):
        """Resolve one contact -> our Client, then upsert all its notes."""
        notes = gh._contact_notes(contact_id)
        if not notes:
            if notes is None:
                stats["note_fetch_errors"] += 1
            return
        stats["contacts_with_notes"] += 1
        mapping = gh._mapping_for(contact_id)
        local_id = mapping.get("local_client_id")
        if not local_id:
            stats["contacts_unmapped"] += 1
            stats["notes_skipped_unmapped"] += len(notes)
            return
        stats["contacts_mapped"] += 1
        for n in notes:
            result = self._upsert_note(n, local_id, dry_run)
            stats[f"notes_{result}"] += 1

    # -- main ---------------------------------------------------------------
    def handle(self, *args, **options):
        if not config.PRIVATE_TOKEN or not config.LOCATION_ID:
            raise CommandError("GHL_PRIVATE_TOKEN and GHL_LOCATION_ID must be set.")

        dry_run = options["dry_run"]
        rate = max(options["rate"], 0.1)

        # Reuse the scan command's GHL plumbing (throttle, search, notes, mapping).
        gh = ScanCommand()
        gh._min_interval = 1.0 / rate
        gh._last_req_at = 0.0
        gh._load_field_catalog()

        stats = Counter()
        mode = "DRY-RUN" if dry_run else "WRITE"
        self.stdout.write(
            f"Importing GHL notes ({mode}); {len(gh._member_id_fields)} member-id "
            f"fields indexed."
        )

        contacts = options["contacts"]
        if contacts:
            for cid in contacts:
                self._process_contact(gh, cid, "", stats, dry_run)
        else:
            self._scan_all(gh, options, stats, dry_run)

        self._report(stats, dry_run)

    def _scan_all(self, gh, options, stats, dry_run):
        page_size = max(1, min(options["page_size"], 100))
        max_contacts = options["max_contacts"]
        search_after = None
        while True:
            page, _total, search_after = gh._search_page(search_after, page_size)
            if not page:
                break
            for contact in page:
                cid = contact.get("id")
                if not cid:
                    continue
                stats["contacts_scanned"] += 1
                self._process_contact(
                    gh, cid, gh._contact_name(contact), stats, dry_run
                )
                n = stats["contacts_scanned"]
                if n % 200 == 0:
                    self.stdout.write(
                        f"  …{n} contacts · {stats['contacts_with_notes']} with "
                        f"notes · {stats['notes_created']} created / "
                        f"{stats['notes_updated']} updated"
                    )
                if max_contacts and n >= max_contacts:
                    self.stdout.write("  reached --max-contacts limit.")
                    return
            if search_after is None:
                break

    def _report(self, stats, dry_run):
        verb = "would import" if dry_run else "imported"
        self.stdout.write(self.style.SUCCESS(
            f"\nDone ({'dry-run' if dry_run else 'write'}). "
            f"{verb}: {stats['notes_created']} created, "
            f"{stats['notes_updated']} updated, "
            f"{stats['notes_skipped']} skipped (empty)."
        ))
        self.stdout.write(
            f"  contacts: {stats['contacts_scanned']} scanned · "
            f"{stats['contacts_with_notes']} with notes · "
            f"{stats['contacts_mapped']} mapped · "
            f"{stats['contacts_unmapped']} unmapped "
            f"({stats['notes_skipped_unmapped']} notes skipped, no external id)."
        )
        if stats["note_fetch_errors"]:
            self.stdout.write(self.style.WARNING(
                f"  {stats['note_fetch_errors']} note-fetch errors."
            ))
