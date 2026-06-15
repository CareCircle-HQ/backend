"""Upsert CallTools users into the local ``Agent`` table.

Maps the CallTools dialer ``extension`` to ``Agent.agent_code`` (the value GHL
and the extension already use) and stores the CallTools identity (app_user uuid,
email, username, role flags) for future features such as authentication.

Match priority: ``calltools_app_user`` (stable id) then ``agent_code``. Existing
agents keep their ``group`` / ``status`` / ``cbo`` (manual classifications);
only identity fields are refreshed. Users without an extension are skipped (no
unique code to authenticate by) and reported.

    python manage.py sync_calltools_agents --dry-run   # preview, no writes
    python manage.py sync_calltools_agents             # apply
    python manage.py sync_calltools_agents --all        # include non-agent users
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.integrations.calltools import agents, client, config
from api.models import Agent


def _norm_ext(value):
    return str(value).strip() if value not in (None, "") else ""


# Non-person CallTools accounts (API keys, vendor/listen-in service accounts).
# Marked Inactive so they're easy to exclude from agent rosters/queries.
_SERVICE_NAME_HINTS = ("apikey", "api key", "vendorapi", " api", "listen in")


def _is_service_account(user, name):
    """True for CallTools entries that aren't real people.

    Heuristic: no agent/manager/owner role flag set, or a name that matches a
    known service-account pattern (e.g. 'Bronx-01.1 APIKEY', 'PitchPerfect API').
    """
    has_role = (
        user.get("is_agent") or user.get("is_manager") or user.get("is_account_owner")
    )
    if not has_role:
        return True
    low = (name or "").lower()
    return any(hint in low for hint in _SERVICE_NAME_HINTS)


class Command(BaseCommand):
    help = "Upsert CallTools users into the local Agent table (extension -> agent_code)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include CallTools users not flagged is_agent.",
        )

    def handle(self, *args, **options):
        if not config.is_enabled():
            self.stderr.write(self.style.ERROR("Set CALLTOOLS_API_TOKEN in .env first."))
            return

        dry = options["dry_run"]
        try:
            users = agents.list_users() if options["all"] else agents.list_agents()
        except client.CallToolsError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        created, updated, skipped = [], [], []
        now = timezone.now()

        for u in users:
            if not isinstance(u, dict):
                continue
            ext = _norm_ext(u.get("extension"))
            full_name = (
                u.get("full_name")
                or " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
                or u.get("username")
                or u.get("email")
                or ""
            )
            app_user = u.get("app_user") or None

            # Match by stable CallTools id first, then by extension (when present).
            agent = None
            if app_user:
                agent = Agent.objects.filter(calltools_app_user=app_user).first()
            if agent is None and ext:
                agent = Agent.objects.filter(agent_code=ext).first()

            # Guard against assigning an extension already taken by another agent.
            if agent is None and ext:
                clash = Agent.objects.filter(agent_code=ext).first()
                if clash is not None:
                    skipped.append((full_name, ext, f"agent_code {ext} already used by {clash.name}"))
                    continue

            identity = dict(
                name=full_name or (agent.name if agent else ""),
                calltools_app_user=app_user,
                email=u.get("email", "") or "",
                username=u.get("username", "") or "",
                first_name=u.get("first_name", "") or "",
                last_name=u.get("last_name", "") or "",
                is_agent=bool(u.get("is_agent")),
                is_manager=bool(u.get("is_manager")),
                is_account_owner=bool(u.get("is_account_owner")),
                calltools_synced_at=now,
            )

            service = _is_service_account(u, full_name)

            if agent is None:
                # New agent: agent_code = extension (NULL when none, so the agent
                # is stored but can't authenticate by code). Classify managers;
                # service/API accounts start Inactive.
                group = "Management" if (u.get("is_manager") or u.get("is_account_owner")) else "Screeners"
                status = "Inactive" if service else "Active"
                if not dry:
                    Agent.objects.create(
                        agent_code=(ext or None), group=group, status=status, **identity
                    )
                created.append((ext or "(no code)", full_name + (" [service]" if service else "")))
            else:
                # Refresh identity only. Status is left alone for real people, but
                # service/API accounts are (re)demoted to Inactive each sync.
                fields = dict(identity)
                if service and agent.status != "Inactive":
                    fields["status"] = "Inactive"
                changed = self._diff(agent, fields)
                if not dry:
                    for k, v in fields.items():
                        setattr(agent, k, v)
                    agent.save(update_fields=list(fields.keys()) + ["updated_at"])
                if changed:
                    updated.append((agent.agent_code or "(no code)", full_name, changed))

        self._report(created, updated, skipped, dry)

    @staticmethod
    def _diff(agent, identity):
        """Return the list of fields whose value would change."""
        changed = []
        for k, v in identity.items():
            if k == "calltools_synced_at":
                continue
            cur = getattr(agent, k)
            cur = str(cur) if cur is not None else ""
            new = str(v) if v is not None else ""
            if cur != new:
                changed.append(k)
        return changed

    def _report(self, created, updated, skipped, dry):
        head = self.style.MIGRATE_HEADING
        prefix = "[DRY RUN] " if dry else ""
        self.stdout.write(
            head(f"\n{prefix}created: {len(created)}  updated: {len(updated)}  skipped: {len(skipped)}")
        )

        if created:
            self.stdout.write(head(f"\nCREATED ({len(created)}):"))
            for ext, name in created:
                self.stdout.write(f"  [{ext:>5}] {name}")

        if updated:
            self.stdout.write(head(f"\nUPDATED ({len(updated)}):"))
            for code, name, fields in updated:
                self.stdout.write(f"  [{code:>5}] {name}  ({', '.join(fields)})")

        if skipped:
            self.stdout.write(self.style.WARNING(f"\nSKIPPED ({len(skipped)}):"))
            for name, ref, reason in skipped:
                self.stdout.write(f"  {name} [{ref}] -- {reason}")

        if dry:
            self.stdout.write(self.style.WARNING("\nDry run: no changes written. Re-run without --dry-run to apply."))
