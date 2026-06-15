"""Fetch the CallTools agent list (and optionally inspect the API schema).

    python manage.py calltools_agents             # list agents
    python manage.py calltools_agents --json       # raw JSON
    python manage.py calltools_agents --schema      # dump available endpoints
    python manage.py calltools_agents --raw /agents/?page_size=5   # probe any path
"""

import json

import requests
from django.core.management.base import BaseCommand

from api.integrations.calltools import agents, client, config


class Command(BaseCommand):
    help = "List CallTools agents (and inspect the authenticated API schema)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print raw JSON.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include non-agent users (managers, owners, API users).",
        )
        parser.add_argument(
            "--schema",
            action="store_true",
            help="Dump the authenticated swagger paths to discover endpoints.",
        )
        parser.add_argument(
            "--raw",
            metavar="PATH",
            help="GET an arbitrary path (relative to the API base) and print JSON.",
        )

    def handle(self, *args, **options):
        # Header goes to stderr so `--json` stdout stays valid JSON for piping.
        self.stderr.write("CallTools configuration:")
        self.stderr.write(f"  API_BASE : {config.API_BASE}")
        self.stderr.write(f"  TOKEN    : {'set' if config.API_TOKEN else '(unset)'}")
        if not config.is_enabled():
            self.stderr.write(
                self.style.ERROR("Set CALLTOOLS_API_TOKEN in .env first.")
            )
            return

        if options.get("schema"):
            return self._dump_schema()
        if options.get("raw"):
            return self._probe(options["raw"])

        include_all = options.get("all")
        try:
            result = agents.list_users() if include_all else agents.list_agents()
        except client.CallToolsError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        if options.get("json"):
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        label = "user(s)" if include_all else "agent(s)"
        self.stdout.write(self.style.SUCCESS(f"\n{len(result)} {label}:"))
        for a in result:
            if not isinstance(a, dict):
                self.stdout.write(f"  - {a}")
                continue
            aid = a.get("app_user") or a.get("id") or "?"
            name = (
                a.get("full_name")
                or " ".join(filter(None, [a.get("first_name"), a.get("last_name")]))
                or a.get("username")
                or a.get("email")
                or "(unnamed)"
            )
            ext = a.get("extension")
            email = a.get("email", "")
            roles = ",".join(
                r for r, on in (
                    ("agent", a.get("is_agent")),
                    ("manager", a.get("is_manager")),
                    ("owner", a.get("is_account_owner")),
                ) if on
            )
            ext_str = f" ext {ext}" if ext else ""
            email_str = f" <{email}>" if email else ""
            role_str = f" ({roles})" if roles else ""
            self.stdout.write(f"  - [{aid}]{ext_str} {name}{email_str}{role_str}")

    def _dump_schema(self):
        """Fetch the authenticated swagger schema and print its endpoints."""
        root = config.API_BASE.rsplit("/api", 1)[0]
        # The swagger endpoint rejects Accept: application/json (406); use */*.
        headers = {**config.headers(), "Accept": "*/*"}
        candidates = [
            f"{root}/api-docs/swagger/?format=openapi",
            f"{root}/api-docs/?format=openapi",
            f"{config.API_BASE}/swagger/?format=openapi",
            f"{config.API_BASE}/schema/?format=openapi",
        ]
        spec = None
        for url in candidates:
            self.stdout.write(f"\nGET {url}")
            try:
                resp = requests.get(url, headers=headers, timeout=config.TIMEOUT)
                resp.raise_for_status()
                spec = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                self.stderr.write(self.style.WARNING(f"  -> {exc}"))
        if spec is None:
            self.stderr.write(self.style.ERROR("Schema fetch failed for all candidates."))
            return
        paths = spec.get("paths") or {}
        if not paths:
            self.stderr.write(
                self.style.WARNING(
                    "No paths returned (token may lack schema access). "
                    "Try --raw /agents/ to probe directly."
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"{len(paths)} endpoint(s):"))
        for p in sorted(paths):
            methods = ",".join(sorted(m.upper() for m in paths[p] if m != "parameters"))
            self.stdout.write(f"  {methods:20} {p}")

    def _probe(self, path):
        try:
            data = client.get(path)
        except client.CallToolsError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return
        self.stdout.write(json.dumps(data, indent=2, default=str)[:4000])
