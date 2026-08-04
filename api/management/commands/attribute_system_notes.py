"""Backfill the author of blank-author SYSTEM notes from the acting agent.

Automated system notes are created alongside a timeline event for the same
action. Many older ones were saved with a blank author (shown as "System" in the
UI). When the action was performed by an AGENT, we can recover who: match the
note to a same-client TimelineEvent within a tight time window whose ``actor``
resolves to a single agent, and stamp that name.

Conservative on purpose:
  * only SYSTEM notes with a blank author are touched,
  * only NON-agent actors (system:*, manual, cron, blank) are ignored -- an
    action with no agent stays "System",
  * a note is attributed ONLY when the window yields exactly ONE agent (no
    guessing between two people).

Dry-run by default; pass --apply to write. --window sets the +/- seconds around
the note's created_at to look for the event (default 300)."""
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from api.models import Agent, Note, NoteSource, TimelineEvent


def resolve_agent_name(actor, *, by_code, names):
    """A TimelineEvent.actor -> an agent display name, or None when it isn't an
    agent (system/import/manual/blank)."""
    a = (actor or "").strip()
    if not a:
        return None
    low = a.lower()
    if low in ("system", "manual", "cron", "import") or low.startswith("system:"):
        return None
    if a.startswith("agent:"):
        return by_code.get(a.split(":", 1)[1].strip()) or None
    if a.startswith("user:"):
        a = a.split(":", 1)[1].strip()
    return a if a in names else None


class Command(BaseCommand):
    help = (
        "Attribute blank-author SYSTEM notes to the acting agent inferred from a "
        "co-timed timeline event. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit the attributions.")
        parser.add_argument("--window", type=int, default=300,
                            help="Seconds +/- the note time to search for the event (default 300).")

    def handle(self, *args, **options):
        apply = options["apply"]
        window = timedelta(seconds=options["window"])

        by_code = {
            a.agent_code: a.name
            for a in Agent.objects.exclude(agent_code="").exclude(agent_code__isnull=True)
            if a.name
        }
        names = {a.name for a in Agent.objects.all() if a.name}

        notes = list(
            Note.objects.filter(source=NoteSource.SYSTEM)
            .filter(Q(author_name="") | Q(author_name__isnull=True))
            .filter(client__isnull=False)
            .only("id", "client_id", "created_at", "author_name")
        )
        attributed = 0
        ambiguous = 0
        no_agent = 0
        by_agent = Counter()
        updates = []
        for n in notes:
            lo, hi = n.created_at - window, n.created_at + window
            actors = (
                TimelineEvent.objects.filter(
                    client_id=n.client_id, created_at__gte=lo, created_at__lte=hi,
                )
                .exclude(actor="")
                .values_list("actor", flat=True)
            )
            found = set()
            for act in actors:
                name = resolve_agent_name(act, by_code=by_code, names=names)
                if name:
                    found.add(name)
            if not found:
                no_agent += 1
            elif len(found) > 1:
                ambiguous += 1
            else:
                name = next(iter(found))
                updates.append((n.id, name))
                by_agent[name] += 1
                attributed += 1

        head = self.style.MIGRATE_HEADING
        self.stdout.write(head("\n=== Attribute blank-author SYSTEM notes ==="))
        self.stdout.write(f"  blank-author SYSTEM notes:      {len(notes)}")
        self.stdout.write(f"  attributable (one agent found): {attributed}")
        self.stdout.write(f"  no agent in window (stay System): {no_agent}")
        self.stdout.write(f"  ambiguous (>1 agent, skipped):  {ambiguous}")
        self.stdout.write("  by inferred agent (top 12):")
        for name, c in by_agent.most_common(12):
            self.stdout.write(f"     {c:6}  {name}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY RUN: nothing changed. Re-run with --apply."))
            return

        done = 0
        for i in range(0, len(updates), 500):
            chunk = updates[i:i + 500]
            with transaction.atomic():
                for nid, name in chunk:
                    Note.objects.filter(pk=nid).update(author_name=name)
                    done += 1
        self.stdout.write(self.style.SUCCESS(f"\nAPPLIED: attributed {done} note(s)."))
