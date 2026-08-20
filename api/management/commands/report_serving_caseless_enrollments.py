"""READ-ONLY triage report for the serving enrollments that are still CASELESS
after the automated consolidation (the ambiguous cases left for manual review --
two live delivery calendars, a case held elsewhere, etc.).

For each one it prints the caseless serving enrollment and every other live
enrollment holding / related to its governing case, with each side's future
delivery-occurrence count, kitchen, next delivery date and member roster, plus a
classification of why it wasn't auto-fixed. Nothing is modified.

    python manage.py report_serving_caseless_enrollments                # all
    python manage.py report_serving_caseless_enrollments --deliveries-only
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import (
    EnrollmentStage,
    EnrollmentVerification,
    OrderSchedule,
)
from api.services.lifecycle import governing_internal_case

_SERVING = {EnrollmentStage.SERVICE_ACTIVE.value, EnrollmentStage.ON_HOLD.value}
_TERMINAL = {
    EnrollmentStage.CLOSED.value,
    EnrollmentStage.CANCELLED.value,
    EnrollmentStage.DISREGARDED.value,
}
_PENDING = EnrollmentStage.PENDING_VERIFICATION.value


class Command(BaseCommand):
    help = "Read-only triage report for still-caseless serving enrollments."

    def add_arguments(self, parser):
        parser.add_argument("--deliveries-only", action="store_true",
                            help="Only list ones that still have future deliveries.")

    def _occ_qs(self, enr):
        return OrderSchedule.objects.filter(
            enrollment=enr, anticipated_delivery_date__gte=timezone.localdate(),
        )

    def _enr_line(self, enr, label):
        occ = self._occ_qs(enr)
        nxt = occ.order_by("anticipated_delivery_date").values_list(
            "anticipated_delivery_date", flat=True).first()
        roster = list(
            enr.member_profiles.values_list("member_name", flat=True)
        )
        return (
            f"      {label} enr {enr.pk} ({enr.stage}) | case "
            f"{str(enr.case_id)[:8] if enr.case_id else '—'} | future deliveries "
            f"{occ.count()} (next {nxt or '—'}) | kitchen "
            f"{enr.kitchen.name if enr.kitchen_id else '—'} | members {roster}"
        )

    def _classify(self, keeper, holders):
        live = [h for h in holders if h.stage not in _TERMINAL]
        if not live:
            return "governing case UNBOUND (no live holder)"
        if any(h.stage in _SERVING and self._occ_qs(h).exists() for h in live) \
                and self._occ_qs(keeper).exists():
            return "BOTH sides have live delivery calendars"
        if any(h.stage in _SERVING for h in live):
            return "another serving enrollment holds the case"
        if any(h.stage == _PENDING for h in live):
            return "governing case on a pending enrollment"
        return "governing case on another live enrollment"

    def handle(self, *args, **opts):
        deliveries_only = opts["deliveries_only"]
        caseless = (
            EnrollmentVerification.objects.filter(case__isnull=True, stage__in=_SERVING)
            .select_related("client", "kitchen")
            .order_by("-opened_at")
        )
        shown = 0
        for keeper in caseless:
            if deliveries_only and not self._occ_qs(keeper).exists():
                continue
            gov = governing_internal_case(keeper)
            holders = list(
                EnrollmentVerification.objects.filter(case=gov).exclude(pk=keeper.pk)
                .select_related("kitchen")
            ) if gov is not None else []
            live_holders = [h for h in holders if h.stage not in _TERMINAL]

            shown += 1
            c = keeper.client
            self.stdout.write(
                f"\n{c.client_id} {c.first_name} {c.last_name} | "
                f"{self._classify(keeper, holders)}"
            )
            gov_desc = (
                f"{str(gov.case_id)[:8]} ({(gov.program_name or gov.service_type or '')[:36]}) "
                f"[{gov.case_status}/{gov.service_authorization_status}]"
                if gov is not None else "—"
            )
            self.stdout.write(f"      governing case: {gov_desc}")
            self.stdout.write(self._enr_line(keeper, "CASELESS serving"))
            for h in (live_holders or holders):
                self.stdout.write(self._enr_line(h, "holder         "))

        self.stdout.write(self.style.SUCCESS(f"\n{shown} enrollment(s) for manual review."))
