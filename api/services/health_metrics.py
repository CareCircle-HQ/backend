"""Publish SERVICE health (not server health) to CloudWatch as custom metrics.

Everything alarmed in Phases 0-3 is technical: is the box up, is it slow, is it
throwing tracebacks. None of it can tell you that nineteen households look active
but cannot receive a delivery, or that the Data page has been stale for a day.
Those only showed up if somebody remembered to run ``manage.py diagnose`` -- which
means the operator is still the monitoring system, just over SSH.

So the same numbers are published as metrics and alarmed on like anything else.
The queries mirror ``diagnose`` deliberately: one source of truth for "is the
service healthy", readable in a terminal AND alarmable.

No new IAM: ``CloudWatchAgentServerPolicy`` (attached for log shipping) already
grants ``cloudwatch:PutMetricData``.

Publishing is OFF unless ``CLOUDWATCH_METRICS_ENABLED`` is set, so local
development and the test suite never reach out to AWS. ``collect()`` is always
safe to call -- it is pure reads -- which is what the management command uses to
show the numbers without publishing them.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

NAMESPACE = "CareCircle/Business"


def unmapped_program_identifiers():
    """Distinct program identifiers on FOOD internal-service cases that map to
    neither meals nor boxes.

    ONE definition, used by both `collect()` and `publish_health_metrics` -- the
    command previously carried its own copy, which then failed to pick up the
    non-food exclusion and would have kept naming housing programs after the
    metric itself was fixed.

    Counts DISTINCT identifiers because the fix is per name: add the keyword to
    `catalog.product_type_kind_for_name`. Cases with NOTHING recorded are excluded
    -- missing data is a different problem with no keyword to add, and
    ServiceTypeBlankWithCase covers it.
    """
    from ..models import Case, CaseType
    from ..services.catalog import product_type_kind_for_name

    from ..services.catalog import non_food_program_names

    non_food_cf = {n.strip().casefold() for n in non_food_program_names()}
    pairs = (
        Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
        .values_list("program_name", "service_type").distinct()
    )
    return sorted({
        (program or service).strip()
        for program, service in pairs
        if (program or service)
        and (program or "").strip().casefold() not in non_food_cf
        and not (
            product_type_kind_for_name(program)
            or product_type_kind_for_name(service)
        )
    })


def collect():
    """The service-health gauges, as ``{metric_name: (value, unit)}``.

    Pure reads, no AWS. Mirrors ``manage.py diagnose``.
    """
    from datetime import timedelta

    from django.db.models import Count, Max

    from ..models import (
        Case, CaseType, EnrollmentAnalytics, EnrollmentStage,
        EnrollmentVerification, ImportRun, ImportRunStatus, ScheduleStatus,
        SERVICE_EXCLUDED_MEMBER_STATUSES, UniteUsCredential,
    )

    now = timezone.now()
    metrics = {}

    # --- Households that look active but cannot be served ------------------
    # The headline number: a member not getting food. `servable` is the subset a
    # calendar rebuild fixes on its own, so it deserves its own metric -- it is
    # ACTIONABLE, where the remainder needs a member returned to service first.
    stranded = list(
        EnrollmentVerification.objects
        .filter(stage=EnrollmentStage.SERVICE_ACTIVE, kitchen__isnull=False)
        .exclude(delivery_schedules__status=ScheduleStatus.SCHEDULED)
        .distinct().prefetch_related("member_profiles")[:500]
    )
    servable_rows = [
        e for e in stranded
        if any(
            p.status not in SERVICE_EXCLUDED_MEMBER_STATUSES
            for p in e.member_profiles.all()
        )
    ]
    # Split by WHO can fix it, because that is the only thing the number is for.
    # A household with delivery_weekdays but no plan is script-repairable
    # (sync_delivery_calendars rebuilds from the cadence). One with NO weekdays
    # cannot be: there is nothing to infer delivery days from, and no script may
    # invent which days a member gets fed -- an agent must assign a cadence.
    #
    # Learned the hard way: both of production's stranded households had
    # weekdays=[], so the "fixable by a calendar rebuild" advice attached to this
    # metric was wrong. sync_delivery_calendars walked all 15,731 enrollments and
    # correctly changed nothing.
    repairable = [e for e in servable_rows if (e.delivery_weekdays or [])]
    metrics["DeliveryGapsNoPlan"] = (len(stranded), "Count")
    metrics["DeliveryGapsServable"] = (len(servable_rows), "Count")
    metrics["DeliveryGapsRepairable"] = (len(repairable), "Count")

    # --- Delivery plans stranded on DEAD enrollments -------------------------
    # A plan belongs to an enrollment; when the enrollment ends its plan should
    # stop serving. Several paths did not do that, and a plan left `scheduled`
    # with a future or NULL window is inert ONLY while the member has no live
    # enrollment. Give them a live one on a different cadence and PO generation
    # serves BOTH: THERESA HOLLAND got Mon/Thu AND Tue/Fri -- four deliveries a
    # week instead of two, for over a month, with nothing to notice it.
    #
    # AT-RISK (member also has a live enrollment) is the number to alarm on; the
    # total is reported alongside because today's inert row is tomorrow's
    # at-risk one, the moment that member is re-enrolled.
    #
    # This metric exists because the code fix CANNOT be complete: one of these
    # was created by a human setting close_reason='serving_duplicate_manual' in a
    # shell. No call-site change catches that, so it has to be detected instead.
    metrics["StrandedDeliveryPlans"] = (stranded_plan_count(), "Count")
    metrics["StrandedDeliveryPlansAtRisk"] = (stranded_plan_count(at_risk=True), "Count")

    # --- Unite Us credential pool ------------------------------------------
    # Expired sessions are how the 2026-09-14 outage began: refreshes walked the
    # whole pool and paid an auth failure for each dead one.
    by_status = dict(
        UniteUsCredential.objects.values_list("status")
        .annotate(n=Count("pk")).values_list("status", "n")
    )
    metrics["CredentialsActive"] = (by_status.get("active", 0), "Count")
    metrics["CredentialsExpired"] = (by_status.get("expired", 0), "Count")

    # --- Read-model freshness ----------------------------------------------
    # The Data page serves this table. When it goes stale the page silently shows
    # yesterday's reality, which no technical alarm can detect.
    #
    # NOTE this reads the REPLICA when one is configured (AnalyticsRouter routes
    # EnrollmentAnalytics reads off the primary), so the number is REBUILD AGE
    # PLUS REPLICATION LAG. Deliberately not pinned to the primary: this is the
    # freshness a user of the Data page actually experiences, which is what the
    # alarm should reflect. Just do not read it as "when did the rebuild finish".
    refreshed = EnrollmentAnalytics.objects.aggregate(t=Max("refreshed_at"))["t"]
    # An EMPTY read model is worse than a stale one -- the Data page has nothing
    # at all. Reporting -1 (or 0) for "never refreshed" would sit below any
    # staleness threshold and read as perfectly healthy, which is the same silent
    # -zero trap as a metric filter that never matches. Report it as very stale so
    # the alarm fires loudly.
    if refreshed is None:
        age_hours = 9999.0
    else:
        # Clamped at 0: ``now`` is captured before this query runs, so while a
        # rebuild is actively writing rows max(refreshed_at) can land a hair AFTER
        # it and the age reads "-0.0". Harmless but confusing in a chart.
        age_hours = max(0.0, (now - refreshed).total_seconds() / 3600)
    metrics["ReadModelAgeHours"] = (round(age_hours, 2), "None")

    # --- Import runs in flight ---------------------------------------------
    # A row stuck PENDING/RUNNING gates "Prepare Members for PO". Age matters more
    # than the count: healthy runs finish in ~10 minutes, so anything hours old is
    # abandoned (now self-healing, but still worth seeing).
    in_flight = ImportRun.objects.filter(
        status__in=(ImportRunStatus.PENDING, ImportRunStatus.RUNNING),
    )
    metrics["ImportRunsInFlight"] = (in_flight.count(), "Count")
    metrics["ImportRunsStuck"] = (
        in_flight.filter(started_at__lt=now - timedelta(hours=2)).count(), "Count",
    )

    # --- Meals/Boxes classification ----------------------------------------
    # Two signals for one failure, because they fire at different times.
    #
    # The failure: an Executive dashboard card shows a total above rows of
    # "meals" and "boxes" that do not sum to it, because some members' product
    # kind is blank. On 2026-09-15 that read "229 pending" over "174 + 36", and it
    # had FOUR separate causes. It was found by a human comparing two numbers on a
    # screen -- which is exactly what a metric is for.
    from ..models import ActiveProgram
    from ..services.catalog import product_type_kind_for_name

    # Both metrics below are about MEALS/BOXES reporting, so non-food internal
    # services must be excluded or they read as defects. Housing programs (type
    # HOUSING, "Environmental Exposure Assessment") are internal services that
    # deliver no meal or box, so they legitimately have no product kind -- without
    # this filter the 3 Dwelling Assessment programs alone put
    # UnmappedProgramNames at 2 and started an alarm that could never clear.
    from ..services.catalog import non_food_program_names

    non_food = non_food_program_names()

    # 1. UPSTREAM, fires the day a new program arrives. product_type_kind_for_name
    #    matches by KEYWORD ("meal"; "box"/"voucher"/"produce prescription"/
    #    "food prescription"/"pantry"/"groceries"), so a future Unite Us program
    #    called e.g. "Nutrition Support Benefit" maps to nothing and silently
    #    starts under-counting. Counts DISTINCT identifiers rather than cases,
    #    because the fix is per name: add the keyword to the catalog.
    #    Only non-empty values: a case with nothing recorded is missing DATA, not
    #    an unrecognised program, and is covered by the second metric.
    metrics["UnmappedProgramNames"] = (
        len(unmapped_program_identifiers()), "Count",
    )

    # 2. DOWNSTREAM, the thing a manager actually sees: read-model rows that HAVE
    #    a case but no product kind, i.e. rows counted in a card's total while
    #    appearing in neither product row.
    #
    #    PINNED TO THE PRIMARY, unlike ReadModelAgeHours above. That one measures
    #    user-visible freshness, so reading the replica is right; this is a
    #    CORRECTNESS check, and reading a lagging replica reports phantom rows --
    #    on 2026-09-15 the same count read 3, then 10, then 21 within minutes and
    #    a repair loop never converged because of it.
    blank_rows = (
        EnrollmentAnalytics.objects.using("default")
        .exclude(company_status="no_case").filter(service_type="")
    )
    if non_food:
        # Same reason: a housing member has no Meals/Boxes kind by design, so they
        # are not a gap in the Meals/Boxes cards. iexact per name rather than __in
        # because the read model's program_name is the CASE's string, which only
        # matched ActiveProgram case-insensitively. Cheap while non-food programs
        # number a handful; revisit if that grows.
        from django.db.models import Q

        q = Q()
        for name in non_food:
            q |= Q(program_name__iexact=name)
        blank_rows = blank_rows.exclude(q)
    metrics["ServiceTypeBlankWithCase"] = (blank_rows.count(), "Count")

    # --- Members needing human review --------------------------------------
    metrics["ReviewBucket"] = (
        EnrollmentAnalytics.objects.filter(company_status="review").count(), "Count",
    )
    return metrics


def publish(metrics=None):
    """Send ``collect()`` to CloudWatch. Returns the number of metrics sent.

    A no-op returning 0 when disabled, and never raises: a monitoring publish
    must not be able to break the caller (this runs on a schedule alongside real
    work). Failures are logged -- and a missing metric shows up as a gap on the
    alarm, which is the right signal.
    """
    if not getattr(settings, "CLOUDWATCH_METRICS_ENABLED", False):
        logger.debug("CloudWatch metrics disabled; not publishing")
        return 0
    metrics = collect() if metrics is None else metrics
    try:
        import boto3

        client = boto3.client(
            "cloudwatch",
            region_name=getattr(settings, "CLOUDWATCH_METRICS_REGION", "us-east-2"),
        )
        now = timezone.now()
        data = [
            {
                "MetricName": name,
                "Value": float(value),
                "Unit": unit,
                "Timestamp": now,
            }
            for name, (value, unit) in sorted(metrics.items())
        ]
        # PutMetricData accepts at most 1000 metrics per call; we send ~9, but
        # chunk anyway so adding more later cannot start silently failing.
        for i in range(0, len(data), 1000):
            client.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 1000])
        logger.info("published %d health metrics to %s", len(data), NAMESPACE)
        return len(data)
    except Exception:
        logger.exception("failed to publish health metrics")
        return 0


def stranded_delivery_plan_enrollments(at_risk=False):
    """Enrollments that are NOT live yet still hold a `scheduled` delivery plan
    whose window has not passed.

    ``at_risk`` narrows to members who ALSO have a live enrollment -- the only
    cohort that can actually double-deliver, since a dead plan with no live
    enrollment generates nothing.

    Shared by the metric and ``retire_dead_enrollment_plans`` so the number the
    alarm reports and the number the sweep fixes cannot drift apart.
    """
    from api.models import (
        EnrollmentStage, EnrollmentVerification, MemberDeliverySchedule,
    )

    live_stages = {
        EnrollmentStage.SERVICE_ACTIVE,
        EnrollmentStage.KITCHEN_ASSIGNMENT,
        EnrollmentStage.ON_HOLD,
    }
    today = timezone.localdate()
    out = {}
    for p in (MemberDeliverySchedule.objects
              .filter(status="scheduled")
              .exclude(ends_on__lt=today)
              .select_related("enrollment")):
        enr = p.enrollment
        if enr is None or EnrollmentStage(enr.stage) in live_stages:
            continue
        out.setdefault(enr.pk, (enr, []))[1].append(p)
    if not at_risk:
        return out
    live_clients = {
        str(c) for c in EnrollmentVerification.objects
        .filter(stage__in=live_stages).values_list("client_id", flat=True) if c
    }
    return {
        k: v for k, v in out.items() if str(v[0].client_id) in live_clients
    }


def stranded_plan_count(at_risk=False):
    """Number of stranded `scheduled` PLANS (not enrollments)."""
    grouped = stranded_delivery_plan_enrollments(at_risk=at_risk)
    return sum(len(ps) for _, ps in grouped.values())
