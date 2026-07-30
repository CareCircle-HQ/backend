"""Requeue households whose delivery calendar was built for the WRONG product
kind after a meals<->boxes governing-case switch that never rebuilt the plan.

Symptom (see the PO-visibility incident): a household's governing internal-
service case switched Meals<->Boxes, but its delivery plan stayed built as the
OLD kind -- so members are served the wrong product (or dropped from the correct
Purchase Order). Detection is name-independent: it compares the plan's BUILT
kind (``plan_built_kind`` -- read from ``meals_per_day`` vs ``prod_per_delivery``
on the MemberDeliverySchedule) against the governing case's DETECTED kind.

Each mis-set household is classified and remediated one of two ways:
  - ``requeue`` -- the kitchen CAN'T make the governing kind, or the plan was
    BUILT as the wrong product. Points ``product_type_override`` at the governing
    kind and calls ``requeue_enrollment_for_product_switch`` (truncate future
    deliveries, clear kitchen + cadence, move to KITCHEN_ASSIGNMENT) so ops
    assign a compatible kitchen + cadence and a fresh calendar.
  - ``rename`` -- the plan/cadence/kitchen are already correct for the governing
    kind; only the ``program_name`` (which PO kind-resolution trusts first) and a
    few leftover off-plan occurrences are stale. Fixes ``enrollment.program_name``
    to the governing program, reconciles the calendar (dropping wrong-day
    leftovers), and re-stamps the name on the scheduled rows.
Both immediately correct which Purchase Order the members land on.

DRY-RUN BY DEFAULT. Pass ``--apply`` to mutate. Scope with explicit enrollment
ids, a client-id file, or ``--all`` to scan every non-terminal enrollment.

Usage:
    python manage.py requeue_switched_households --all                 # dry-run scan
    python manage.py requeue_switched_households --all --apply          # apply
    python manage.py requeue_switched_households 29541 29446            # dry-run these
    python manage.py requeue_switched_households 29541 29446 --apply    # apply these
"""
from django.core.management.base import BaseCommand, CommandError

from api.models import EnrollmentVerification
from api.services.lifecycle import (
    classify_switched_household,
    remediate_switched_household,
)


class Command(BaseCommand):
    help = "Requeue households whose delivery plan kind disagrees with the governing case."

    def add_arguments(self, parser):
        parser.add_argument(
            "enrollment_ids", nargs="*", type=int,
            help="Explicit EnrollmentVerification pks to check.",
        )
        parser.add_argument("--all", action="store_true", help="Scan all non-terminal enrollments.")
        parser.add_argument("--apply", action="store_true", help="Mutate (default is dry-run).")
        parser.add_argument("--limit", type=int, default=0, help="Cap rows printed/acted on.")

    def _candidates(self, opts):
        ids = opts.get("enrollment_ids") or []
        if ids:
            return EnrollmentVerification.objects.filter(pk__in=ids)
        if opts.get("all"):
            # Only enrollments that actually hold a delivery plan can be
            # mis-built; the requeue itself no-ops on terminal stages.
            return EnrollmentVerification.objects.filter(
                delivery_schedules__isnull=False
            ).distinct()
        raise CommandError("Pass enrollment ids or --all.")

    def handle(self, *args, **opts):
        apply = opts.get("apply")
        limit = opts.get("limit") or 0
        w = self.stdout.write
        head = self.style.MIGRATE_HEADING

        found = []
        for enr in self._candidates(opts).select_related("kitchen"):
            gov, action, reason = classify_switched_household(enr)
            if action is not None:
                found.append((enr, gov, action, reason))

        w(head(
            f"\n{'APPLY' if apply else 'DRY-RUN'}: households needing remediation: "
            f"{len(found)}"
        ))
        rows = found if limit <= 0 else found[:limit]
        counts = {"rename": 0, "requeue": 0}
        for enr, gov, action, reason in rows:
            k = enr.kitchen
            w(
                f"  enr={enr.pk} client={enr.client_id} stage={enr.stage} "
                f"| governing={getattr(gov, 'value', gov)} action={action} ({reason}) "
                f"kitchen={getattr(k, 'name', None)} "
                f"supports={getattr(k, 'supported_products', None)}"
            )
            if not apply:
                continue
            # Delegate to the shared self-heal (same code the 'Prepare Members
            # for PO' sweep runs), so the CLI and the button never diverge.
            done = remediate_switched_household(
                enr, actor=None, actor_label="system:po-switch-remediation",
            )
            if done in counts:
                counts[done] += 1
            w(f"    -> {done or 'no-op'}")

        if apply:
            w(head(
                f"\nRenamed {counts['rename']}, requeued {counts['requeue']} "
                f"enrollment(s)."
            ))
        else:
            w(head("\nDry-run only. Re-run with --apply."))
        if limit and len(found) > limit:
            w(f"({len(found) - limit} more not shown; raise --limit.)")
