"""Daily Unite Us pull orchestration (spec §2–§6).

Strategy: fetch from the Unite Us core API, map each JSON:API record into the
dict shape our existing DRF serializers already accept, then upsert through those
serializers — reusing their idempotent ``update_or_create`` + reconcile logic.
Everything runs inside ``change_context(IMPORT, ...)`` so every history row is
tagged source='import'. Each record is isolated (one bad row can't roll back the
run), counts roll up into an ``ImportRun``, and follow-up ``Ticket`` rows are
raised per spec §6.

Person discovery note: a provider-wide cases/people listing endpoint is not yet
confirmed (prompt §9 pending), so this refreshes the Clients we already store.
New-member discovery is a follow-up once that endpoint is known.
"""

import hashlib
import logging
from collections import defaultdict

from django.utils import timezone

from api.history import ChangeSource, change_context
from api.integrations.uniteus import api as uu_api
from api.integrations.uniteus import mappers
from api.integrations.uniteus.api import (
    MEDICAL_PLAN_TYPES,
    UniteUsApiError,
    UniteUsAuthExpired,
)
from api.models import (
    Case,
    CaseType,
    Client,
    ContractedService,
    ImportRun,
    ImportRunStatus,
    Insurance,
    Note,
    SocialCareCoverage,
    UniteUsCredential,
    UniteUsCredentialStatus,
)
from api.serializers import (
    CaseSerializer,
    ClientSerializer,
    ContractedServiceSerializer,
)
from api.services import tickets, timeline

logger = logging.getLogger(__name__)


# Tone hints (frontend colors) for the on-demand refresh "what changed" list.
_AUTH_CHANGE_TONE = {
    "approved": "success", "not_required": "success",
    "pending": "warning", "expired": "warning", "denied": "danger",
}
_STATUS_CHANGE_TONE = {"cancelled": "danger"}


def _pretty(code):
    return (code or "").replace("_", " ").title() or "—"


class DailyPull:
    def __init__(self, run, emit_side_effects=False):
        self.run = run
        # When False (the default for cron + on-demand refresh), the pull syncs
        # data and keeps the derived state fresh (enrollment reconciliation,
        # funnel stage, Care Management warnings) but does NOT write audit
        # timeline events or open follow-up tickets -- Care Management (member
        # warnings) is the source of truth for what needs attention, and the
        # per-record timeline writes were the bulk of the pull's DB load.
        self.emit_side_effects = emit_side_effects
        self.api = None
        self.name_cache = {}
        self.errors = []
        self.stats = defaultdict(lambda: defaultdict(int))
        # On-demand refresh: human-readable list of what THIS run changed
        # (auth transitions, case-status transitions, new Medicaid insurance).
        # Persisted to run.stats["changes"] and surfaced by refresh_from_uniteus.
        self.changes = []

    # -- counters ----------------------------------------------------------
    def _count(self, dataset, kind):
        self.stats[dataset][kind] += 1

    # -- refresh change tracking -------------------------------------------
    _CHANGES_CAP = 50

    def _add_change(self, **item):
        if len(self.changes) < self._CHANGES_CAP:
            self.changes.append(item)

    def _record_case_changes(self, case, prev_status, prev_auth, result):
        """Translate a CaseChangeResult into refresh 'what changed' items."""
        program = case.program_name or "Internal service case"
        if result.auth_changed:
            new = case.service_authorization_status or ""
            self._add_change(
                kind="authorization",
                case_id=str(case.case_id),
                program=program,
                to=new,
                label=f"Authorization: {_pretty(prev_auth)} → {_pretty(new)}",
                tone=_AUTH_CHANGE_TONE.get(new, "info"),
            )
        if result.status_changed:
            self._add_change(
                kind="case_status",
                case_id=str(case.case_id),
                program=program,
                to=case.case_status or "",
                label=f"Case status: {_pretty(prev_status)} → {case.get_case_status_display()}",
                tone=_STATUS_CHANGE_TONE.get(case.case_status, "info"),
            )

    def _record_new_medicaid(self, client, new_insurance_ids):
        """Add a change item for each newly-added Medicaid insurance."""
        from api.models import Insurance, InsurancePlanType

        meds = Insurance.objects.filter(
            client=client, insurance_id__in=list(new_insurance_ids),
            plan_type__in=(InsurancePlanType.MEDICAID, InsurancePlanType.DUAL),
        )
        for ins in meds:
            self._add_change(
                kind="insurance_medicaid",
                label="New Medicaid insurance added",
                detail=ins.plan_name or "",
                tone="success",
            )

    def _save(self, serializer_cls, data, dataset, existed):
        try:
            ser = serializer_cls(data=data)
            ser.is_valid(raise_exception=True)
            obj = ser.save()
            self._count(dataset, "updated" if existed else "created")
            return obj
        except Exception as exc:  # noqa: BLE001 - isolate one bad record
            self._count(dataset, "errors")
            self.errors.append(f"{dataset} upsert failed ({data.get('client_id') or data.get('case_id') or data.get('contracted_service_id')}): {exc}")
            logger.warning("daily_pull %s upsert failed: %s", dataset, exc)
            return None

    def _emit_timeline(self, builder, obj):
        """Emit a timeline event, isolating failures from the import run."""
        try:
            builder(obj, source=ChangeSource.IMPORT, actor="system:unite-us-import")
        except Exception:  # noqa: BLE001
            logger.warning("timeline emit failed (%s)", type(obj).__name__, exc_info=True)

    # -- related-name resolution (cached per run) --------------------------
    def _name(self, resource_path, rid, attrs=("name", "full_name")):
        if not rid:
            return ""
        if rid in self.name_cache:
            return self.name_cache[rid]
        name = ""
        try:
            body = self.api.get_resource(resource_path, rid)
            a = (body.get("data") or {}).get("attributes") or {}
            name = next((a[c] for c in attrs if a.get(c)), "")
        except UniteUsApiError:
            pass
        self.name_cache[rid] = name
        return name

    # -- coverage ----------------------------------------------------------
    def _pull_coverage(self, client_id):
        med = self.api.list_insurances(client_id, MEDICAL_PLAN_TYPES)
        soc = self.api.list_insurances(client_id, "social")
        medicaid = self.api.list_insurances(client_id, "medicaid")
        plan_ids = [mappers._rel_id(r, "plan") for r in (med + soc)]
        plan_info = self.api.get_plans([p for p in plan_ids if p])
        medicaid_ids = {
            mappers._rel_id(r, "plan") for r in medicaid if mappers._rel_id(r, "plan")
        }
        ins = [mappers.map_insurance_record(r, plan_info, medicaid_ids) for r in med]
        scc = [mappers.map_coverage_record(r, plan_info, medicaid_ids) for r in soc]
        return (
            [i for i in ins if i["plan_name"]],
            [s for s in scc if s["plan_name"]],
        )

    # -- notes -------------------------------------------------------------
    def _process_notes(self, subject_id, client, case):
        subject_type = "Case" if case is not None else "Person"
        try:
            records = self.api.list_notes(subject_id, subject_type)
        except UniteUsApiError as exc:
            self.errors.append(f"notes for {subject_id}: {exc}")
            return
        for rec in records:
            data = mappers.map_note(
                rec,
                client_id=client.pk if client else None,
                case_id=case.pk if case else None,
            )
            sid = data.get("source_note_id")
            try:
                if sid:
                    _, created = Note.objects.get_or_create(
                        source="unite_us", source_note_id=sid, defaults=data
                    )
                else:
                    h = hashlib.sha256(
                        f"{data.get('body', '')}|{data.get('source_created_at')}".encode()
                    ).hexdigest()
                    data["content_hash"] = h
                    _, created = Note.objects.get_or_create(
                        content_hash=h, defaults=data
                    )
                self._count("notes", "created" if created else "skipped")
            except Exception as exc:  # noqa: BLE001
                self._count("notes", "errors")
                self.errors.append(f"note {sid}: {exc}")

    # -- contracted services ----------------------------------------------
    def _process_contracted_services(self, case):
        case_id = str(case.case_id)
        try:
            provided = self.api.list_provided_services(case_id)
        except UniteUsApiError as exc:
            self.errors.append(f"provided_services for {case_id}: {exc}")
            return
        if not provided:
            return
        # The provided_service carries no auth link; apply the case's sole auth.
        sole_auth = None
        try:
            auths = self.api.list_service_authorizations(case_id)
            if len(auths) == 1:
                sole_auth = mappers._attrs(auths[0])
        except UniteUsApiError:
            pass
        program_name = case.program_name or ""
        for ps in provided:
            invoice = None
            inv_ids = mappers._rel_ids(ps, "invoices")
            if inv_ids:
                try:
                    inv_body = self.api.get_invoice(inv_ids[-1])
                    invoice = mappers._attrs(inv_body.get("data") or {})
                except UniteUsApiError:
                    pass
            data = mappers.map_provided_service(
                ps, case_id=case_id, sole_auth=sole_auth, invoice=invoice,
                program_name=program_name,
            )
            existed = ContractedService.objects.filter(pk=data["contracted_service_id"]).exists()
            self._save(ContractedServiceSerializer, data, "contracted_services", existed)

    # -- cases -------------------------------------------------------------
    def _process_case(self, case_rec, client):
        names = {
            "service": self._name("/services", mappers._rel_id(case_rec, "service")),
            "program": self._name("/programs", mappers._rel_id(case_rec, "program")),
            "program_id": mappers._rel_id(case_rec, "program"),
            "network": self._name("/networks", mappers._rel_id(case_rec, "network")),
            "network_id": mappers._rel_id(case_rec, "network"),
            "primary_worker": self._name(
                "/employees", mappers._rel_id(case_rec, "primary_worker"), ("full_name", "name")
            ),
            "primary_worker_id": mappers._rel_id(case_rec, "primary_worker"),
        }
        auth_attrs = None
        auth_id = mappers._rel_id(case_rec, "service_authorization")
        if auth_id:
            try:
                auth_attrs = mappers._attrs(
                    self.api.get_service_authorization(auth_id).get("data") or {}
                )
            except UniteUsApiError:
                pass

        prev = Case.objects.filter(pk=case_rec.get("id")).first()
        prev_status = prev.case_status if prev else None
        prev_auth = prev.service_authorization_status if prev else None

        data = mappers.map_case(case_rec, names=names, auth=auth_attrs)
        case = self._save(CaseSerializer, data, "cases", existed=bool(prev))
        if case is None:
            return
        self._process_contracted_services(case)
        self._process_notes(subject_id=str(case.case_id), client=client, case=case)
        # Record the case change (timeline events + follow-up tickets) via the
        # shared handler. Contracted services are loaded just above, so the
        # case_no_services rule is meaningful here -- no skip (unlike CSV imports).
        from api.services import case_events

        change = case_events.record_case_change(
            case, previous_status=prev_status, previous_auth=prev_auth,
            source=ChangeSource.IMPORT, actor="system:unite-us-import",
            create_tickets=self.emit_side_effects,
            emit_timeline=self.emit_side_effects, import_run=self.run,
        )
        self._record_case_changes(case, prev_status, prev_auth, change)
        self._reconcile_enrollments(case)
        if self.emit_side_effects:
            self._emit_timeline(timeline.event_for_case, case)

    def _reconcile_enrollments(self, case):
        """Project the (possibly updated) case authorization onto any verified
        enrollments. When the status flips to Accepted, this is what triggers
        delivery-order generation during the nightly run. Best-effort: a single
        enrollment hiccup must not abort the import."""
        from api.services.lifecycle import reconcile_enrollment_authorization

        for enrollment in case.enrollments.all():
            try:
                reconcile_enrollment_authorization(enrollment)
            except Exception as exc:  # pragma: no cover - defensive
                self.errors.append(f"reconcile enrollment {enrollment.pk}: {exc}")

    # -- person / client ---------------------------------------------------
    def _process_person(self, client_id):
        person = self.api.get_person(client_id)
        data = person.get("data") or {}
        if not data:
            if self.emit_side_effects:
                tickets.evaluate_member_not_found(client_id, self.run)
            self._count("clients", "skipped")
            return

        consent = None
        consent_id = mappers._rel_id(data, "consent")
        if consent_id:
            try:
                consent = mappers.map_consent(self.api.get_consent(consent_id))
            except UniteUsApiError:
                pass

        client_dict = mappers.map_person_to_client(person, consent=consent)
        ins_dicts, scc_dicts = self._pull_coverage(client_id)
        if ins_dicts:
            client_dict["insurances"] = ins_dicts
        if scc_dicts:
            client_dict["social_care_coverages"] = scc_dicts

        existed = Client.objects.filter(pk=client_id).first()
        pre_ins = (
            set(Insurance.objects.filter(client_id=client_id).values_list("insurance_id", flat=True))
            if existed else set()
        )
        pre_scc = (
            set(SocialCareCoverage.objects.filter(client_id=client_id).values_list("coverage_id", flat=True))
            if existed else set()
        )

        client = self._save(ClientSerializer, client_dict, "clients", existed=bool(existed))
        if client is None:
            return

        post_ins = set(Insurance.objects.filter(client=client).values_list("insurance_id", flat=True))
        new_ins_ids = post_ins - pre_ins
        if new_ins_ids:
            if self.emit_side_effects:
                tickets.evaluate_new_insurance(client, self.run)
            # Always record new Medicaid for the refresh summary (no DB write).
            self._record_new_medicaid(client, new_ins_ids)
        post_scc = set(SocialCareCoverage.objects.filter(client=client).values_list("coverage_id", flat=True))
        if post_scc - pre_scc and self.emit_side_effects:
            tickets.evaluate_new_coverage(client, self.run)

        if self.emit_side_effects:
            tickets.evaluate_client_coverage(client, self.run)

            # Timeline events for the consent + each insurance / coverage record.
            self._emit_timeline(timeline.event_for_consent, client)
            for ins in Insurance.objects.filter(client=client):
                self._emit_timeline(timeline.event_for_insurance, ins)
            for scc in SocialCareCoverage.objects.filter(client=client):
                self._emit_timeline(timeline.event_for_social_care_coverage, scc)

        self._process_notes(subject_id=client_id, client=client, case=None)

        for case_rec in self.api.list_cases(client_id):
            self._process_case(case_rec, client)

        # Recompute the acquisition funnel now that consent + cases are synced.
        try:
            from api.services.lifecycle import recompute_client_stage

            recompute_client_stage(client)
        except Exception:  # noqa: BLE001 - never abort the import on a funnel hiccup
            logger.warning("recompute_client_stage failed for %s", client_id, exc_info=True)

    # -- entry -------------------------------------------------------------
    def execute(self, client_limit=None, provider_id=None, client_ids=None):
        creds = UniteUsCredential.objects.filter(
            status=UniteUsCredentialStatus.ACTIVE
        ).defer("access_token", "refresh_token")  # decrypt lazily, per-credential
        if provider_id:
            creds = creds.filter(provider_id=provider_id)
        creds = list(creds)
        if not creds:
            self.errors.append("No active Unite Us credentials; nothing to pull.")
            return

        if client_ids:
            client_ids = [str(c) for c in client_ids]
        else:
            # Only refresh members who have at least one internal-service case --
            # those are the ones whose Unite Us case status / authorization can
            # change. Members with no internal-service case have nothing for the
            # pull to update, so skipping them avoids needless API calls.
            client_ids = [
                str(c)
                for c in Client.objects.filter(
                    cases__case_type=CaseType.INTERNAL_SERVICE
                )
                .distinct()
                .values_list("client_id", flat=True)
            ]
            if client_limit:
                client_ids = client_ids[:client_limit]

        for cred in creds:
            # Lazily decrypt the token columns now (they were deferred above); a
            # corrupt / rotated-key credential is skipped (logged on the run)
            # rather than crashing the whole nightly run.
            try:
                _ = cred.refresh_token
            except Exception as exc:  # noqa: BLE001
                # Integration/auth problem, not CS work: record it on the run +
                # logs only. Do NOT open a customer-support work-queue ticket.
                logger.warning("daily_pull credential %s unreadable: %s", cred.pk, exc)
                self.errors.append(f"credential {cred.pk} unreadable: {exc}")
                continue
            self.api = uu_api.UniteUsClient(cred)
            self.name_cache = {}
            try:
                for cid in client_ids:
                    try:
                        self._process_person(cid)
                    except UniteUsAuthExpired:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        self._count("clients", "errors")
                        self.errors.append(f"person {cid}: {exc}")
            except UniteUsAuthExpired as exc:
                # Expired Unite Us login is an integration issue (an agent must
                # re-login via the extension), not CS work: log + record on the
                # run, no customer-support ticket.
                logger.warning("daily_pull credential %s expired: %s", cred.pk, exc)
                self.errors.append(f"credential {cred.pk} expired: {exc}")
                continue

    def finalize(self):
        agg = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        for dataset, kinds in self.stats.items():
            for kind, n in kinds.items():
                if kind in agg:
                    agg[kind] += n
        self.run.stats = {d: dict(k) for d, k in self.stats.items()}
        if self.changes:
            # Appended LAST so next(iter(stats)) -- the dataset label the Import
            # Activity UI reads -- still resolves to a real dataset, not "changes".
            self.run.stats["changes"] = self.changes
        self.run.created_count = agg["created"]
        self.run.updated_count = agg["updated"]
        self.run.skipped_count = agg["skipped"]
        self.run.error_count = agg["errors"]
        self.run.processed_count = sum(agg.values())


def run_daily_pull(*, triggered_by="cron", client_limit=None, provider_id=None,
                   client_ids=None):
    """Execute one daily pull and return the persisted ImportRun."""
    run = ImportRun.objects.create(
        source="uniteus", status=ImportRunStatus.RUNNING, triggered_by=triggered_by
    )
    puller = DailyPull(run)
    try:
        with change_context(ChangeSource.IMPORT, "system:unite-us-import"):
            puller.execute(
                client_limit=client_limit, provider_id=provider_id,
                client_ids=client_ids,
            )
        run.status = ImportRunStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001
        run.status = ImportRunStatus.FAILED
        puller.errors.append(f"FATAL: {exc}")
        logger.exception("daily_pull aborted")
    finally:
        puller.finalize()
        run.finished_at = timezone.now()
        if puller.errors:
            run.error_log = "\n".join(puller.errors)[:10000]
        run.save()
    return run


# ---------------------------------------------------------------------------
# On-demand refresh (agent-triggered from the CRM "Refresh from Unite Us"
# button). Reuses the exact daily-pull pipeline so a refresh runs the same
# map -> serializer -> case_events -> reconcile_enrollment_authorization path,
# meaning an authorization that flipped to Accepted advances the enrollment and
# generates deliveries just like the nightly run.
# ---------------------------------------------------------------------------

# Substrings that mean "the Unite Us session is unusable -> the agent must
# re-login in the browser so the extension re-captures a token" (vs a transient
# per-record error). Matched case-insensitively against the run's error log.
_RECONNECT_MARKERS = ("expired", "unusable", "not usable", "no active unite us")


def _select_active_credential(provider_id=None):
    """Pick the credential for an on-demand refresh: the most-recently-captured
    active one (freshest token), optionally scoped to a provider.

    Defers the encrypted token columns so selecting a credential never eagerly
    decrypts -- a wrong/rotated FIELD_ENCRYPTION_KEY is surfaced later (in
    _refresh_single_case, wrapped) as a clean 'reconnect' result, not a 500."""
    qs = UniteUsCredential.objects.filter(
        status=UniteUsCredentialStatus.ACTIVE
    ).defer("access_token", "refresh_token")
    if provider_id:
        qs = qs.filter(provider_id=provider_id)
    return qs.order_by("-last_captured_at", "-updated_at").first()


def _refresh_single_case(puller, client_id, case_id, provider_id):
    """Refresh ONE internal-service case through the full per-case pipeline."""
    cred = _select_active_credential(provider_id=provider_id)
    if cred is None:
        puller.errors.append("No active Unite Us credentials; nothing to pull.")
        return
    try:
        _ = cred.refresh_token  # force decrypt; skip a corrupt/rotated-key cred
    except Exception as exc:  # noqa: BLE001
        puller.errors.append(f"credential {cred.pk} unusable: {exc}")
        return

    puller.api = uu_api.UniteUsClient(cred)
    puller.name_cache = {}
    client = Client.objects.filter(pk=client_id).first()
    if client is None:
        puller.errors.append(f"client {client_id} not found locally")
        return

    records = puller.api.list_cases(client_id)
    rec = next((r for r in records if str(r.get("id")) == str(case_id)), None)
    if rec is None:
        puller.errors.append(
            f"case {case_id} not returned by Unite Us for client {client_id} "
            "(it may be closed or outside the managed/pending filter)"
        )
        return

    puller._process_case(rec, client)
    # Keep the acquisition funnel consistent after an approval flips the case.
    try:
        from api.services.lifecycle import recompute_client_stage

        recompute_client_stage(client)
    except Exception:  # noqa: BLE001
        logger.warning("recompute_client_stage failed for %s", client_id, exc_info=True)


def _summarize_refresh(run, *, scope):
    """Normalize an ImportRun into the dict the portal endpoint returns."""
    err = (run.error_log or "").lower()
    needs_reconnect = any(m in err for m in _RECONNECT_MARKERS)
    changes = (run.stats or {}).get("changes") or []
    ok = (
        run.status == ImportRunStatus.COMPLETED
        and run.error_count == 0
        and not needs_reconnect
    )
    if needs_reconnect:
        message = (
            "Unite Us session expired. Open Unite Us in the browser to reconnect, "
            "then try again."
        )
    elif not ok:
        message = "Refresh finished with errors. Check the import log."
    elif changes:
        n = len(changes)
        message = f"Refreshed from Unite Us — {n} update{'s' if n != 1 else ''} detected."
    else:
        message = "Refreshed from Unite Us. No changes."
    return {
        "ok": ok,
        "scope": scope,
        "needs_reconnect": needs_reconnect,
        "message": message,
        "changes": changes,
        "import_run_id": run.pk,
        "status": run.status,
        "created": run.created_count,
        "updated": run.updated_count,
        "skipped": run.skipped_count,
        "errors": run.error_count,
        "error_log": run.error_log or "",
    }


def refresh_from_uniteus(client_id, *, case_id=None, provider_id=None,
                         triggered_by="portal:refresh"):
    """On-demand Unite Us refresh for one client.

    With ``case_id`` only that internal-service case is refreshed; otherwise the
    whole member (all cases + coverage + notes) via :func:`run_daily_pull`.
    Returns a summary dict (see :func:`_summarize_refresh`) whose
    ``needs_reconnect`` flag tells the UI when the agent must re-login to Unite Us.
    """
    client_id = str(client_id)

    if not case_id:
        run = run_daily_pull(
            triggered_by=triggered_by, provider_id=provider_id,
            client_ids=[client_id],
        )
        return _summarize_refresh(run, scope="member")

    run = ImportRun.objects.create(
        source="uniteus", status=ImportRunStatus.RUNNING, triggered_by=triggered_by,
    )
    puller = DailyPull(run)
    try:
        with change_context(ChangeSource.IMPORT, "system:unite-us-import"):
            _refresh_single_case(puller, client_id, str(case_id), provider_id)
        run.status = ImportRunStatus.COMPLETED
    except UniteUsAuthExpired as exc:
        run.status = ImportRunStatus.FAILED
        puller.errors.append(f"credential expired: {exc}")
        logger.warning("refresh single case: credential expired: %s", exc)
    except Exception as exc:  # noqa: BLE001
        run.status = ImportRunStatus.FAILED
        puller.errors.append(f"FATAL: {exc}")
        logger.exception("refresh single case aborted")
    finally:
        puller.finalize()
        run.finished_at = timezone.now()
        if puller.errors:
            run.error_log = "\n".join(puller.errors)[:10000]
        run.save()
    return _summarize_refresh(run, scope="case")
