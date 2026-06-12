"""Compare the CallTools agent roster against the local ``Agent`` table.

The join key is the CallTools ``extension`` matched against ``Agent.agent_code``
(e.g. Denzell Ferrier = 355 in both systems).

    python manage.py calltools_agent_diff            # agents only (is_agent)
    python manage.py calltools_agent_diff --all        # include non-agent users
    python manage.py calltools_agent_diff --json        # machine-readable output
"""

import json

from django.core.management.base import BaseCommand

from api.integrations.calltools import agents, client, config
from api.models import Agent


def _norm(value):
    return str(value).strip() if value not in (None, "") else ""


def _ct_name(u):
    return (
        u.get("full_name")
        or " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
        or u.get("username")
        or u.get("email")
        or "(unnamed)"
    )


class Command(BaseCommand):
    help = "Compare CallTools agents (by extension) with the local Agent table (by agent_code)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include CallTools users not flagged is_agent.",
        )
        parser.add_argument("--json", action="store_true", help="Emit JSON.")

    def handle(self, *args, **options):
        if not config.is_enabled():
            self.stderr.write(self.style.ERROR("Set CALLTOOLS_API_TOKEN in .env first."))
            return

        try:
            ct_users = agents.list_users() if options["all"] else agents.list_agents()
        except client.CallToolsError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        # Index CallTools users by extension (string). Track those with none.
        ct_by_ext = {}
        ct_no_ext = []
        for u in ct_users:
            if not isinstance(u, dict):
                continue
            ext = _norm(u.get("extension"))
            (ct_by_ext.__setitem__(ext, u) if ext else ct_no_ext.append(u))

        db_by_code = {_norm(a.agent_code): a for a in Agent.objects.all()}

        ct_codes, db_codes = set(ct_by_ext), set(db_by_code)
        order = lambda c: (len(c), c)
        matched = sorted(ct_codes & db_codes, key=order)
        only_ct = sorted(ct_codes - db_codes, key=order)
        only_db = sorted(db_codes - ct_codes, key=order)

        if options["json"]:
            self.stdout.write(json.dumps(self._as_dict(
                ct_by_ext, db_by_code, matched, only_ct, only_db, ct_no_ext
            ), indent=2, default=str))
            return

        self._print(ct_by_ext, db_by_code, matched, only_ct, only_db, ct_no_ext, len(ct_users))

    # -- output helpers -----------------------------------------------------
    def _as_dict(self, ct_by_ext, db_by_code, matched, only_ct, only_db, ct_no_ext):
        return {
            "matched": [
                {
                    "agent_code": c,
                    "db_name": db_by_code[c].name,
                    "ct_name": _ct_name(ct_by_ext[c]),
                    "ct_email": ct_by_ext[c].get("email", ""),
                    "name_mismatch": db_by_code[c].name.strip().lower()
                    != _ct_name(ct_by_ext[c]).strip().lower(),
                }
                for c in matched
            ],
            "only_in_calltools": [
                {"extension": c, "name": _ct_name(ct_by_ext[c]), "email": ct_by_ext[c].get("email", "")}
                for c in only_ct
            ],
            "only_in_db": [
                {"agent_code": c, "name": db_by_code[c].name, "group": db_by_code[c].group,
                 "status": db_by_code[c].status}
                for c in only_db
            ],
            "calltools_without_extension": [
                {"name": _ct_name(u), "email": u.get("email", ""), "app_user": u.get("app_user")}
                for u in ct_no_ext
            ],
        }

    def _print(self, ct_by_ext, db_by_code, matched, only_ct, only_db, ct_no_ext, total_ct):
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR
        self.stdout.write(
            f"\nCallTools users fetched: {total_ct}  |  local Agents: {len(db_by_code)}"
        )
        self.stdout.write(
            ok(f"  matched: {len(matched)}") + "  "
            + warn(f"only in CallTools: {len(only_ct)}") + "  "
            + warn(f"only in DB: {len(only_db)}")
        )

        mismatches = [
            c for c in matched
            if db_by_code[c].name.strip().lower() != _ct_name(ct_by_ext[c]).strip().lower()
        ]
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nMATCHED ({len(matched)}):"))
        for c in matched:
            db_name, ct_name = db_by_code[c].name, _ct_name(ct_by_ext[c])
            flag = warn("  <- name differs") if c in mismatches else ""
            self.stdout.write(f"  [{c:>5}] db='{db_name}'  ct='{ct_name}'{flag}")

        self.stdout.write(self.style.MIGRATE_HEADING(f"\nONLY IN CALLTOOLS ({len(only_ct)}):"))
        for c in only_ct:
            u = ct_by_ext[c]
            self.stdout.write(f"  [{c:>5}] {_ct_name(u)}  <{u.get('email','')}>")

        self.stdout.write(self.style.MIGRATE_HEADING(f"\nONLY IN DB ({len(only_db)}):"))
        for c in only_db:
            a = db_by_code[c]
            self.stdout.write(f"  [{c:>5}] {a.name}  ({a.group}, {a.status})")

        if ct_no_ext:
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"\nCALLTOOLS WITHOUT EXTENSION ({len(ct_no_ext)}):")
            )
            for u in ct_no_ext:
                self.stdout.write(f"  {_ct_name(u)}  <{u.get('email','')}>")
