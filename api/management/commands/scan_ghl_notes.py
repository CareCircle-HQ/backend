"""Phase 0 of the GHL notes pull: a read-only scan that measures the real
volume + content of GoHighLevel timeline Contact Notes across the whole
location, WITHOUT needing any member->contact mapping.

It pages every contact (``POST /contacts/search`` with ``searchAfter`` deep
pagination), calls ``GET /contacts/{id}/notes`` for each, and streams every note
to a JSON Lines dump (one note per line, flushed as it goes so a long run isn't
lost if interrupted). A ``*.summary.json`` sidecar records the run totals.

    # sample first (600 contacts is enough to hit the first note-bearing ones)
    python manage.py scan_ghl_notes --max-contacts 600

    # full scan (36k+ contacts -> ~75 min at 8 req/s; safe to leave running)
    python manage.py scan_ghl_notes

    python manage.py scan_ghl_notes --out tmp/ghl_notes_dump.jsonl --rate 8

Read-only against GHL; it does NOT honor CRM_SYNC_DISCONNECTED (that gate only
guards OUTBOUND writes). It only needs a valid GHL_PRIVATE_TOKEN (+ location).

Each dumped note line carries:
    id, contactId, contactName, title, bodyText, body_len, dateAdded, userId,
    pinned

Notes are documented as sparse: the default search order front-loads old,
note-less lead contacts, so the first note-bearing contact only appeared after
~635 contacts in earlier testing. Use ``--max-contacts`` for a quick smoke test,
then a full run for real numbers.
"""
import json
import os
import time
from collections import Counter

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.integrations.ghl import config

# LeadConnector burst limit is ~100 requests / 10s; default to a safe 8 req/s.
DEFAULT_RATE = 8.0
# Retry ceiling for transient 429/5xx before giving up on a single request.
MAX_RETRIES = 4


class Command(BaseCommand):
    help = (
        "Read-only scan of GHL timeline Contact Notes across the whole location "
        "(measures volume/content for the notes-pull Phase 0). Dumps JSONL."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default="",
            help="Output JSONL path (default tmp/ghl_notes_dump.jsonl).",
        )
        parser.add_argument(
            "--max-contacts",
            type=int,
            default=0,
            help="Stop after scanning N contacts (0 = all). Use for a smoke test.",
        )
        parser.add_argument(
            "--rate",
            type=float,
            default=DEFAULT_RATE,
            help=f"Max requests/second throttle (default {DEFAULT_RATE}).",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=100,
            help="Contacts per search page (GHL max 100).",
        )

    # -- throttled HTTP -----------------------------------------------------
    def _throttle(self):
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_req_at)
        if wait > 0:
            time.sleep(wait)
        self._last_req_at = time.monotonic()

    def _request(self, method, url, **kwargs):
        """Throttled request with retry/backoff on 429 + 5xx. Returns the
        Response, or None when it exhausted retries / hit a transport error."""
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = requests.request(
                    method, url, headers=config.headers(),
                    timeout=config.TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    self.stderr.write(self.style.WARNING(f"  {method} {url}: {exc}"))
                    return None
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    return resp
                # Honor Retry-After when present, else linear backoff.
                delay = float(resp.headers.get("Retry-After") or 1.5 * (attempt + 1))
                time.sleep(delay)
                continue
            return resp
        return None

    # -- GHL calls ----------------------------------------------------------
    def _search_page(self, search_after, page_size):
        """One page of the location's contacts. Returns (contacts, total,
        next_search_after)."""
        url = f"{config.API_BASE}/contacts/search"
        body = {"locationId": config.LOCATION_ID, "pageLimit": page_size}
        if search_after:
            body["searchAfter"] = search_after
        resp = self._request("POST", url, json=body)
        if resp is None:
            return [], None, None
        if resp.status_code == 401:
            raise CommandError(
                "GHL returned 401 Unauthorized. The GHL_PRIVATE_TOKEN is missing, "
                "expired, or lacks the contacts scope."
            )
        if resp.status_code != 200:
            raise CommandError(
                f"POST {url} -> {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        contacts = data.get("contacts") or []
        total = data.get("total")
        next_after = contacts[-1].get("searchAfter") if contacts else None
        return contacts, total, next_after

    def _contact_notes(self, contact_id):
        """List a contact's timeline notes. Returns a list (empty on any
        non-200, which we count but don't treat as fatal)."""
        url = f"{config.API_BASE}/contacts/{contact_id}/notes"
        resp = self._request("GET", url)
        if resp is None or resp.status_code != 200:
            return None  # signal an error (distinct from an empty list)
        return resp.json().get("notes") or []

    @staticmethod
    def _contact_name(contact):
        name = (contact.get("contactName") or "").strip()
        if name:
            return name
        first = contact.get("firstName") or ""
        last = contact.get("lastName") or ""
        return f"{first} {last}".strip()

    # -- main ---------------------------------------------------------------
    def handle(self, *args, **options):
        if not config.PRIVATE_TOKEN:
            raise CommandError("GHL_PRIVATE_TOKEN is not set; configure it in .env.")
        if not config.LOCATION_ID:
            raise CommandError("GHL_LOCATION_ID is not set; configure it in .env.")

        rate = max(options["rate"], 0.1)
        self._min_interval = 1.0 / rate
        self._last_req_at = 0.0
        page_size = max(1, min(options["page_size"], 100))
        max_contacts = options["max_contacts"]

        out_path = options["out"] or os.path.join(
            settings.BASE_DIR, "tmp", "ghl_notes_dump.jsonl"
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        stats = Counter()
        color_tally = Counter()
        started = timezone.now()
        search_after = None
        total_reported = None
        first_note_after = None  # #contacts scanned before the first note appeared

        self.stdout.write(
            f"Scanning GHL notes -> {out_path} (rate {rate}/s, page {page_size})"
        )

        with open(out_path, "w", encoding="utf-8") as fh:
            try:
                while True:
                    contacts, total, search_after = self._search_page(
                        search_after, page_size
                    )
                    if total is not None:
                        total_reported = total
                    if not contacts:
                        break

                    for contact in contacts:
                        cid = contact.get("id")
                        if not cid:
                            continue
                        stats["contacts_scanned"] += 1

                        notes = self._contact_notes(cid)
                        if notes is None:
                            stats["note_fetch_errors"] += 1
                        elif notes:
                            stats["contacts_with_notes"] += 1
                            if first_note_after is None:
                                first_note_after = stats["contacts_scanned"]
                            cname = self._contact_name(contact)
                            for n in notes:
                                stats["notes_total"] += 1
                                if n.get("pinned"):
                                    stats["notes_pinned"] += 1
                                color_tally[n.get("color") or ""] += 1
                                body = n.get("body") or ""
                                line = {
                                    "id": n.get("id"),
                                    "contactId": n.get("contactId") or cid,
                                    "contactName": cname,
                                    "title": n.get("title") or "",
                                    "bodyText": n.get("bodyText") or "",
                                    "body_len": len(body),
                                    "dateAdded": n.get("dateAdded") or "",
                                    "userId": n.get("userId") or "",
                                    "pinned": bool(n.get("pinned")),
                                }
                                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                            fh.flush()

                        n = stats["contacts_scanned"]
                        if n % 200 == 0:
                            self.stdout.write(
                                f"  …{n} contacts · {stats['contacts_with_notes']} "
                                f"with notes · {stats['notes_total']} notes"
                            )
                        if max_contacts and n >= max_contacts:
                            raise _Done
                    if search_after is None:
                        break  # no cursor -> reached the end
            except _Done:
                self.stdout.write("  reached --max-contacts limit.")
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("\n  interrupted; partial dump kept."))

        summary = {
            "started_at": started.isoformat(),
            "finished_at": timezone.now().isoformat(),
            "location_id": config.LOCATION_ID,
            "location_total_contacts_reported": total_reported,
            "contacts_scanned": stats["contacts_scanned"],
            "contacts_with_notes": stats["contacts_with_notes"],
            "notes_total": stats["notes_total"],
            "notes_pinned": stats["notes_pinned"],
            "note_fetch_errors": stats["note_fetch_errors"],
            "first_note_after_n_contacts": first_note_after,
            "note_colors": dict(color_tally),
            "dump_path": out_path,
            "max_contacts": max_contacts or None,
            "rate_per_sec": rate,
        }
        summary_path = out_path.rsplit(".", 1)[0] + ".summary.json"
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False, default=str)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {stats['contacts_scanned']} contacts scanned · "
            f"{stats['contacts_with_notes']} with notes · "
            f"{stats['notes_total']} notes ({stats['notes_pinned']} pinned) · "
            f"{stats['note_fetch_errors']} fetch errors."
        ))
        if total_reported:
            self.stdout.write(f"  location reports {total_reported} total contacts.")
        if first_note_after:
            self.stdout.write(f"  first note appeared after {first_note_after} contacts.")
        self.stdout.write(f"  dump    : {out_path}")
        self.stdout.write(f"  summary : {summary_path}")


class _Done(Exception):
    """Internal sentinel to break out of the nested scan loop cleanly."""
