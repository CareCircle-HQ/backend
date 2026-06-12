"""List the GoHighLevel opportunity pipelines (and stages) for the configured
location -- i.e. the ones the CURRENT token/location can actually use.

Use this to confirm whether the pipeline IDs in tmp/pipelines_id.csv belong to
the same location as GHL_LOCATION_ID (sandbox vs production mismatch is the
usual cause of "The pipeline id is invalid").

    python manage.py ghl_pipelines
"""

import requests
from django.core.management.base import BaseCommand

from api.integrations.ghl import config

# IDs currently hard-coded in opportunities.py, for a quick match check.
KNOWN_IDS = {
    "ENJvUOcoV0fQWX36V8Rq": "B: Screening (code)",
    "F6cAYzGyB9H1Tsb88QZO": "C: Eligibility (code)",
    "05nsZFCbcujvqSJIdlbN": "G1: Food Delivery / case (code)",
    "2GToxmnm3MrMsotZ1kgn": "D: Navigation (code)",
    "vVnLwzTO1nkVxUt0zmdF": "E: External Services (code)",
    "ld0HoLxCzj8ooiuOm8hX": "F: Attestation (code)",
}


class Command(BaseCommand):
    help = "List GHL opportunity pipelines + stages for the configured location."

    def handle(self, *args, **options):
        self.stdout.write(f"LOCATION_ID: {config.LOCATION_ID or '(unset)'}")
        self.stdout.write(f"TOKEN      : {'set' if config.PRIVATE_TOKEN else '(unset)'}")
        if not config.PRIVATE_TOKEN or not config.LOCATION_ID:
            self.stderr.write(self.style.ERROR("Set GHL_PRIVATE_TOKEN + GHL_LOCATION_ID first."))
            return

        url = f"{config.API_BASE}/opportunities/pipelines"
        params = {"locationId": config.LOCATION_ID}
        self.stdout.write(f"\nGET {url}?locationId={config.LOCATION_ID}\n")
        try:
            resp = requests.get(
                url, headers=config.headers(), params=params, timeout=config.TIMEOUT
            )
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"Request failed: {exc}"))
            return

        if resp.status_code != 200:
            self.stderr.write(self.style.ERROR(f"Status {resp.status_code}: {resp.text[:500]}"))
            return

        pipelines = resp.json().get("pipelines", [])
        if not pipelines:
            self.stderr.write(self.style.WARNING("No pipelines returned for this location."))
            return

        live_ids = set()
        self.stdout.write(self.style.SUCCESS(f"{len(pipelines)} pipeline(s) for this location:\n"))
        for p in pipelines:
            pid = p.get("id", "")
            live_ids.add(pid)
            self.stdout.write(self.style.MIGRATE_HEADING(f"{p.get('name', '?')}"))
            self.stdout.write(f"  pipelineId: {pid}")
            for stage in p.get("stages", []):
                self.stdout.write(f"    stage: {stage.get('id')}  {stage.get('name')}")
            self.stdout.write("")

        # Compare against the IDs hard-coded in the code.
        self.stdout.write(self.style.MIGRATE_HEADING("Code pipeline IDs vs this location:"))
        for cid, label in KNOWN_IDS.items():
            ok = cid in live_ids
            mark = self.style.SUCCESS("FOUND") if ok else self.style.ERROR("MISSING")
            self.stdout.write(f"  [{mark}] {cid}  {label}")
        self.stdout.write(
            "\nAny MISSING id belongs to a different location (likely the other "
            "of sandbox/production). Update opportunities.py with the FOUND ids, "
            "or point GHL_LOCATION_ID/token at the location that owns these ids."
        )
