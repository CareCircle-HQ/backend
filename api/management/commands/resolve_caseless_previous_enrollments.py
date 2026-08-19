"""Resolve the backlog of CASELESS "previous enrollments" -- superseded rows
(``close_reason='case_replaced'``) that were left with no case FK.

Every enrollment must reference its case; these legacy rows violate that. For
each caseless previous enrollment we look at the client's OTHER internal-service
cases (excluding the survivor's current case) and act per bucket:

  * 0 candidate prior cases -> FLAG ``hidden_misinformation`` (a pre-case
    placeholder with no distinct case to attach; hidden from the UI, purge later).
  * exactly 1 candidate      -> BACKFILL: bind that prior case onto the row and
    record it as the survivor's ``previous_case``.
  * 2+ candidates            -> AMBIGUOUS: printed for manual review, unchanged.

With ``--resolve-ambiguous-by-close-match`` an ambiguous row is auto-resolved
when EXACTLY ONE candidate's case CLOSED on the survivor case's created date --
i.e. the case the survivor directly replaced. True ties (no such candidate, or
more than one) are still left for manual review.

DRY-RUN by default (prints what WOULD change); pass ``--apply`` to commit. Use
``--client <id>`` to scope to one member. Idempotent.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, CaseType, EnrollmentVerification


class Command(BaseCommand):
    help = (
        "Flag caseless placeholder previous enrollments (no prior case), backfill "
        "the unambiguous ones, and list the ambiguous ones (dry-run; --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Commit the changes (default is a dry-run that changes nothing).",
        )
        parser.add_argument(
            "--client", default="",
            help="Only process this client_id (default: every affected client).",
        )
        parser.add_argument(
            "--resolve-ambiguous-by-close-match", action="store_true",
            help=(
                "For 2+-candidate rows, bind the candidate that CLOSED on the "
                "survivor case's created date (the case it directly replaced) when "
                "exactly one such candidate exists; leave true ties for review."
            ),
        )
        parser.add_argument(
            "--bind", action="append", default=[], metavar="ENR=CASE_ID",
            help=(
                "Manual override for a specific ambiguous row: bind CASE_ID onto "
                "enrollment ENR (the case must belong to that client). Repeatable."
            ),
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        only = (opts.get("client") or "").strip()
        close_match = opts["resolve_ambiguous_by_close_match"]
        overrides = {}  # enr_pk -> case_id
        for spec in opts["bind"]:
            enr_str, _, cid = spec.partition("=")
            if not enr_str.strip().isdigit() or not cid.strip():
                self.stderr.write(f"Ignoring malformed --bind '{spec}' (want ENR=CASE_ID)")
                continue
            overrides[int(enr_str.strip())] = cid.strip()

        superseded_ids = set(
            EnrollmentVerification.objects
            .filter(supersedes__isnull=False)
            .values_list("supersedes_id", flat=True)
        )
        prev = (
            EnrollmentVerification.objects
            .filter(pk__in=superseded_ids, case__isnull=True, close_reason="case_replaced")
            .select_related("client")
        )
        if only:
            prev = prev.filter(client__client_id=only)

        flagged = backfilled = close_matched = manual = 0
        ambiguous = []  # (client_id, enr_pk, [candidate case ids])

        def _bind(enrollment, prior, survivor):
            """Bind ``prior`` onto the enrollment + record it as the survivor's
            previous_case. No-op unless --apply."""
            if not apply:
                return
            with transaction.atomic():
                enrollment.case = prior
                fields = ["case"]
                if not enrollment.program_name and prior.program_name:
                    enrollment.program_name = prior.program_name
                    fields.append("program_name")
                if not enrollment.service_type and prior.service_type:
                    enrollment.service_type = prior.service_type
                    fields.append("service_type")
                enrollment.save(update_fields=fields)
                if survivor is not None and survivor.previous_case_id is None:
                    survivor.previous_case = prior
                    survivor.save(update_fields=["previous_case"])

        for e in prev.iterator(chunk_size=500):
            survivor = EnrollmentVerification.objects.filter(supersedes=e).first()
            surv_case = survivor.case if survivor else None

            # Manual override wins over every bucket rule below.
            if e.pk in overrides:
                forced = Case.objects.filter(
                    pk=overrides[e.pk], client_id=e.client_id,
                    case_type=CaseType.INTERNAL_SERVICE,
                ).first()
                if forced is None:
                    self.stderr.write(
                        f"--bind {e.pk}={overrides[e.pk]} skipped: not an internal-"
                        f"service case for client {e.client_id}"
                    )
                else:
                    manual += 1
                    _bind(e, forced, survivor)
                continue

            candidates = list(
                Case.objects
                .filter(client_id=e.client_id, case_type=CaseType.INTERNAL_SERVICE)
                .exclude(case_id=(surv_case.case_id if surv_case else None))
                .order_by("-case_created_at", "-date_opened")
            )

            if not candidates:
                flagged += 1
                if apply:
                    e.hidden_misinformation = True
                    e.save(update_fields=["hidden_misinformation"])
                continue

            if len(candidates) == 1:
                backfilled += 1
                _bind(e, candidates[0], survivor)
                continue

            # 2+ candidates. Optionally auto-resolve to the case the survivor
            # directly replaced: the one that CLOSED on the survivor's created
            # date. Only when exactly one candidate matches (else it's a real tie).
            if close_match and surv_case is not None:
                surv_created = _as_date(surv_case.case_created_at) or _as_date(surv_case.date_opened)
                matches = [
                    c for c in candidates
                    if surv_created is not None and _as_date(c.case_closed_at) == surv_created
                ]
                if len(matches) == 1:
                    close_matched += 1
                    _bind(e, matches[0], survivor)
                    continue

            ambiguous.append(
                (str(e.client_id), e.pk, [str(c.case_id) for c in candidates])
            )

        if ambiguous:
            self.stdout.write("AMBIGUOUS (still need manual review):")
            self.stdout.write(f"  {'client_id':<38}{'enr':<8}candidate_case_ids")
            for cid, enr_pk, cases in ambiguous:
                self.stdout.write(f"  {cid:<38}{enr_pk:<8}{', '.join(cases)}")

        mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}: {flagged} flagged as misinformation (no prior case), "
            f"{backfilled} backfilled to their single prior case"
            + (f", {manual} bound via --bind" if manual else "")
            + (f", {close_matched} ambiguous auto-resolved by close-match" if close_match else "")
            + f", {len(ambiguous)} ambiguous (left for review)."
        ))


def _as_date(dt):
    """The date component of a datetime/date, or None."""
    if dt is None:
        return None
    return dt.date() if hasattr(dt, "date") else dt
