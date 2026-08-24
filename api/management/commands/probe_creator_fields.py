"""Probe the Unite Us APIs for the CREATOR / FACILITATOR identity on cases,
assessments, and screenings -- to confirm whether we can source accountability
attribution headlessly (the agent using the extension is NOT the creator).

For one ``--client <person_id>`` it fetches:
  * core ``/cases`` (JSON:API)        -- dumps a case record's attribute +
                                         relationship keys and any creator-ish
                                         field (created_by / submitter /
                                         requestor / author / screener ...),
  * screenings-ingestion assessments  -- list summary + SurveyJS detail keys,
  * screenings-ingestion screenings    -- list summary + detail keys,
highlighting the fields the CSV export calls ``case_created_by_id/name``,
``submission_created_by_id/name`` and ``facilitator_id`` so we can see whether
the live API carries them.

READ-ONLY: prints only, makes NO local changes, and (by default) never rotates
the shared single-use refresh token. Run with a freshly captured credential:

    python manage.py probe_creator_fields --client <person_id>
    python manage.py probe_creator_fields --client <person_id> --refresh
"""

import json

from django.core.management.base import BaseCommand

# Substrings that hint at a creator/author/facilitator identity anywhere in a
# record (case-insensitive). Kept broad on purpose -- the point is discovery.
_CANDIDATE_HINTS = (
    "created_by", "created_at", "creator", "submitter", "submission",
    "submitted_by", "requestor", "requested_by", "author", "facilitator",
    "screener", "screened_by", "employee", "performed_by", "user",
)


def _looks_creatorish(key):
    k = str(key).lower()
    return any(h in k for h in _CANDIDATE_HINTS)


def _scan(obj, prefix=""):
    """Yield (dotted_path, value) for creator-ish keys, one level into nested
    dicts (JSON:API attributes/relationships are shallow enough for this)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}{k}"
            if _looks_creatorish(k):
                # Summarize value compactly.
                if isinstance(v, (dict, list)):
                    yield path, json.dumps(v)[:200]
                else:
                    yield path, v
            if isinstance(v, dict):
                yield from _scan(v, prefix=f"{path}.")


class Command(BaseCommand):
    help = "Probe Unite Us APIs for case/assessment/screening creator fields (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Unite Us person_id to probe.")
        parser.add_argument("--provider-id", default=None, help="Use the credential for this provider.")
        parser.add_argument("--refresh", action="store_true",
                            help="Allow a server-side token refresh (rotates the shared refresh token).")
        parser.add_argument("--dump", action="store_true",
                            help="Also print the full raw JSON of the first record of each type.")

    def handle(self, *args, **opts):
        from api.integrations.uniteus import api as uu_api
        from api.integrations.uniteus.api import UniteUsApiError, UniteUsAuthExpired
        from api.integrations.uniteus.screenings_api import ScreeningsIngestionClient
        from api.services.uniteus_import import _select_active_credential

        person_id = opts["client"]
        allow_refresh = bool(opts["refresh"])
        dump = bool(opts["dump"])

        cred = _select_active_credential(provider_id=opts["provider_id"])
        if cred is None:
            self.stderr.write(self.style.ERROR(
                "No active Unite Us credential; open Unite Us in the browser to "
                "capture one, then re-run."
            ))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Probe creator fields (person={person_id}) ==="
        ))
        self.stdout.write(
            f"  cred={cred.pk} provider_id={getattr(cred, 'provider_id', None)} "
            f"allow_refresh={allow_refresh}\n"
        )

        self._probe_cases(uu_api, cred, allow_refresh, person_id, dump,
                          UniteUsApiError, UniteUsAuthExpired)
        self._probe_ingestion(ScreeningsIngestionClient, cred, allow_refresh,
                             person_id, dump, UniteUsApiError, UniteUsAuthExpired)

    # -- cases (core JSON:API) --------------------------------------------
    def _probe_cases(self, uu_api, cred, allow_refresh, person_id, dump,
                     UniteUsApiError, UniteUsAuthExpired):
        self.stdout.write(self.style.HTTP_INFO("\n--- CASES (core /cases) ---"))
        api = uu_api.UniteUsClient(cred, allow_refresh=allow_refresh)
        try:
            body = api.core_get(
                "/cases",
                params={
                    "filter[person]": person_id,
                    "filter[include_pathways]": "false",
                    "sort": "updated_at",
                    "sort_direction": "desc",
                    "page[number]": 1,
                    "page[size]": 5,
                },
            )
        except (UniteUsApiError, UniteUsAuthExpired) as exc:
            self.stderr.write(self.style.ERROR(f"  cases request failed: {exc}"))
            return
        data = (body or {}).get("data") or []
        self.stdout.write(f"  cases returned: {len(data)}")
        if not data:
            return
        rec = data[0]
        attrs = rec.get("attributes") or {}
        rels = rec.get("relationships") or {}
        self.stdout.write(f"  attribute keys   : {sorted(attrs.keys())}")
        self.stdout.write(f"  relationship keys: {sorted(rels.keys())}")
        hits = list(_scan(attrs)) + list(_scan({"relationships": rels}))
        self.stdout.write(self.style.SUCCESS("  creator-ish fields:"))
        if hits:
            for path, val in hits:
                self.stdout.write(f"    {path} = {val}")
        else:
            self.stdout.write("    (none found in the list record -- try a detail "
                              "GET /cases/<id> or include=created_by)")
        if dump:
            self.stdout.write(json.dumps(rec, indent=2)[:4000])

    # -- assessments + screenings (screenings-ingestion) -------------------
    def _probe_ingestion(self, ClientCls, cred, allow_refresh, person_id, dump,
                         UniteUsApiError, UniteUsAuthExpired):
        client = ClientCls(cred, allow_refresh=allow_refresh)
        for label, lister in (
            ("ASSESSMENTS", client.list_assessments),
            ("SCREENINGS", client.list_screenings),
        ):
            self.stdout.write(self.style.HTTP_INFO(
                f"\n--- {label} (screenings-ingestion) ---"
            ))
            try:
                rows = lister(person_id)
            except (UniteUsApiError, UniteUsAuthExpired) as exc:
                self.stderr.write(self.style.ERROR(f"  {label} list failed: {exc}"))
                continue
            self.stdout.write(f"  {label.lower()} returned: {len(rows)}")
            if not rows:
                continue
            summary = rows[0]
            self.stdout.write(f"  summary keys: {sorted(summary.keys())}")
            hits = list(_scan(summary))
            self.stdout.write(self.style.SUCCESS("  creator-ish fields (summary):"))
            for path, val in hits:
                self.stdout.write(f"    {path} = {val}")
            if not hits:
                self.stdout.write("    (none in summary)")
            # Detail
            try:
                detail = client.get_screen_detail(summary.get("id"))
            except (UniteUsApiError, UniteUsAuthExpired) as exc:
                self.stderr.write(self.style.ERROR(f"  {label} detail failed: {exc}"))
                detail = {}
            if detail:
                self.stdout.write(f"  detail keys: {sorted(detail.keys())}")
                dhits = list(_scan(detail))
                self.stdout.write(self.style.SUCCESS("  creator-ish fields (detail):"))
                for path, val in dhits:
                    self.stdout.write(f"    {path} = {val}")
                if not dhits:
                    self.stdout.write("    (none in detail)")
            if dump:
                self.stdout.write(json.dumps({"summary": summary, "detail": detail}, indent=2)[:4000])
