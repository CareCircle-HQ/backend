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


def collect():
    """The service-health gauges, as ``{metric_name: (value, unit)}``.

    Pure reads, no AWS. Mirrors ``manage.py diagnose``.
    """
    from datetime import timedelta

    from django.db.models import Count, Max

    from ..models import (
        EnrollmentAnalytics, EnrollmentStage, EnrollmentVerification, ImportRun,
        ImportRunStatus, ScheduleStatus, SERVICE_EXCLUDED_MEMBER_STATUSES,
        UniteUsCredential,
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
    servable = sum(
        1 for e in stranded
        if any(
            p.status not in SERVICE_EXCLUDED_MEMBER_STATUSES
            for p in e.member_profiles.all()
        )
    )
    metrics["DeliveryGapsNoPlan"] = (len(stranded), "Count")
    metrics["DeliveryGapsServable"] = (servable, "Count")

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
