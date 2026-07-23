"""One-off backfill (2026-07-23): pull unapproved-but-active members back to
Waiting Authorization.

Households that were activated (Kitchen Assignment / Service Active) while their
governing internal-service authorization is only PENDING / never-requested /
blank must not be in service -- only an approval authorizes delivery. The
reconcile chokepoint now enforces this going forward (the "pull-back" rule in
``reconcile_internal_service_authorization``); this command applies the same
rule to the EXISTING backlog by re-running that idempotent reconcile once per
affected client.

Effect per client: each Kitchen-Assignment / Service-Active enrollment whose
governing authorization isn't approved returns to Verified (displayed as
"Waiting Authorization"), its future deliveries are truncated (dropping it off
Purchase Orders), and the whole household follows. Auto-resumes when the case is
later approved.

Dry-run by default (lists what WOULD change). Pass --apply to commit.
"""
import csv

from django.core.management.base import BaseCommand

from api.models import EnrollmentStage, EnrollmentVerification
from api.services.lifecycle import (
    _WAITING_AUTH_STATUSES,
    governing_internal_case,
    open_internal_service_cases,
    reconcile_internal_service_authorization,
)

_PULLBACK_STAGES = [EnrollmentStage.KITCHEN_ASSIGNMENT, EnrollmentStage.SERVICE_ACTIVE]


def _candidate_enrollments():
    """Post-verification enrollments whose governing internal-service
    authorization is not approved AND whose client still has an open
    internal-service case (a client with no open case is handled by the
    close-out rule, not this one)."""
    out = []
    qs = (
        EnrollmentVerification.objects.filter(stage__in=_PULLBACK_STAGES)
        .select_related("client")
    )
    for e in qs:
        gov = governing_internal_case(e)
        auth = getattr(gov, "service_authorization_status", "") if gov else ""
        if auth not in _WAITING_AUTH_STATUSES:
            continue
        client = e.client
        if client is None or not open_internal_service_cases(client):
            continue
        out.append((e, auth))
    return out


class Command(BaseCommand):
    help = (
        "Pull unapproved-but-active enrollments back to Waiting Authorization "
        "(runs the reconcile per affected client). Dry-run by default; --apply "
        "to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually apply. Without this the command only previews.")
        parser.add_argument("--csv", default="",
                            help="Optional path to write the candidate report.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        candidates = _candidate_enrollments()
        by_client = {}
        for e, auth in candidates:
            by_client.setdefault(e.client_id, []).append((e, auth))

        self.stdout.write(
            f"Found {len(candidates)} enrollment(s) across {len(by_client)} "
            f"client(s) advanced past Verified with an unapproved authorization."
        )
        for e, auth in candidates:
            c = e.client
            name = f"{c.first_name} {c.last_name}".strip() if c else "?"
            self.stdout.write(
                f"  enr {e.pk} | {name} ({str(e.client_id)[:8]}) | "
                f"{e.stage} -> verified | gov_auth={auth or 'BLANK'}"
            )

        if opts["csv"]:
            with open(opts["csv"], "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["enrollment_id", "client_id", "client_name", "stage", "gov_auth"])
                for e, auth in candidates:
                    c = e.client
                    w.writerow([
                        e.pk, e.client_id,
                        f"{c.first_name} {c.last_name}".strip() if c else "",
                        e.stage, auth or "BLANK",
                    ])
            self.stdout.write(self.style.SUCCESS(f"\nWrote report to {opts['csv']}"))

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run -- no changes made. Re-run with --apply to commit."
            ))
            return

        downgraded_clients = 0
        for client_id in by_client:
            client = by_client[client_id][0][0].client
            res = reconcile_internal_service_authorization(client)
            if res.get("downgraded"):
                downgraded_clients += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: reconciled {len(by_client)} client(s); "
            f"{downgraded_clients} had enrollments downgraded to Waiting Authorization."
        ))
