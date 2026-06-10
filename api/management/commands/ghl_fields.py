"""Dump the GoHighLevel custom field catalog for the configured location.

Used to map our ``Client`` model onto GHL contact custom fields (e.g.
"Enrollment Platform Client ID" -> client_id). Writes the raw JSON to a file
and prints a compact, sorted table.

    python manage.py ghl_fields
    python manage.py ghl_fields --out ghl_custom_fields.json
"""

import json

import requests
from django.core.management.base import BaseCommand

from api.integrations.ghl import config


class Command(BaseCommand):
    help = "List GoHighLevel contact custom fields for the configured location."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            choices=["contact", "opportunity"],
            default="contact",
            help="Which custom field model to list.",
        )
        parser.add_argument(
            "--out",
            default="",
            help="File to write the raw JSON to (defaults to ghl_<model>_fields.json).",
        )
        parser.add_argument(
            "--pipelines",
            action="store_true",
            help="Also dump opportunity pipelines + stages.",
        )

    def handle(self, *args, **options):
        if not config.PRIVATE_TOKEN or not config.LOCATION_ID:
            self.stderr.write(self.style.ERROR("Missing token or location id."))
            return

        model = options["model"]
        out = options["out"] or f"ghl_{model}_fields.json"

        url = f"{config.API_BASE}/locations/{config.LOCATION_ID}/customFields"
        resp = requests.get(
            url, headers=config.headers(), params={"model": model},
            timeout=config.TIMEOUT,
        )
        if resp.status_code != 200:
            self.stderr.write(
                self.style.ERROR(f"{resp.status_code}: {resp.text[:500]}")
            )
            return

        fields = [
            f for f in resp.json().get("customFields", [])
            if f.get("model") == model
        ]
        fields.sort(key=lambda f: f.get("name", ""))

        with open(out, "w", encoding="utf-8") as fh:
            json.dump(fields, fh, indent=2)

        self.stdout.write(
            f"{len(fields)} {model} custom fields. Raw JSON written to {out}.\n"
        )
        self.stdout.write(f"{'NAME':<45} {'FIELD KEY':<45} {'TYPE':<16} ID")
        self.stdout.write("-" * 130)
        for f in fields:
            name = (f.get("name") or "")[:44]
            key = (f.get("fieldKey") or "")[:44]
            dtype = (f.get("dataType") or "")[:15]
            self.stdout.write(f"{name:<45} {key:<45} {dtype:<16} {f.get('id')}")

        if options["pipelines"]:
            self._dump_pipelines()

    def _dump_pipelines(self):
        url = f"{config.API_BASE}/opportunities/pipelines"
        resp = requests.get(
            url, headers=config.headers(),
            params={"locationId": config.LOCATION_ID}, timeout=config.TIMEOUT,
        )
        if resp.status_code != 200:
            self.stderr.write(
                self.style.ERROR(f"pipelines {resp.status_code}: {resp.text[:300]}")
            )
            return
        pipelines = resp.json().get("pipelines", [])
        with open("ghl_pipelines.json", "w", encoding="utf-8") as fh:
            json.dump(pipelines, fh, indent=2)
        self.stdout.write(
            f"\n{len(pipelines)} pipelines (written to ghl_pipelines.json):"
        )
        for p in pipelines:
            self.stdout.write(f"\n  {p.get('name')}  [{p.get('id')}]")
            for s in p.get("stages", []):
                self.stdout.write(f"    - {s.get('name'):<40} {s.get('id')}")
