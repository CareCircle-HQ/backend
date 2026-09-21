"""Booking a dispatch visit: overlap checks, reminders and the member's text.

TIMEZONES ARE THE WHOLE DIFFICULTY HERE. A vendor picks "9:00 AM" meaning nine
o'clock at the member's front door, so every time in this module is built in the
member's local zone (``settings.TIME_ZONE``, America/New_York) and only then
converted to an aware datetime. Reading or writing these as UTC dates is how a
visit ends up booked a day out -- the same boundary that has four tests failing
after 20:00 EDT.
"""
import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from ..models import (
    DispatchReminder, DispatchStatus, DispatchVisit, MessageKind, ReminderAudience,
    ReminderKind, StageEventSource,
)

logger = logging.getLogger(__name__)

# What a vendor may choose as a session length. Half-hour granularity, because the
# member is told an arrival window and a finer number would imply a precision
# nobody can hold to.
ALLOWED_DURATIONS = (30, 60, 90, 120, 180, 240)
DEFAULT_DURATION = 60

# How long before a visit each reminder fires.
REMINDER_OFFSETS = {
    ReminderKind.DAY_BEFORE: timedelta(days=1),
    ReminderKind.THIRTY_MIN: timedelta(minutes=30),
}


class SchedulingError(Exception):
    """A booking that must not go ahead, with a code the app can branch on."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def local_tz():
    return timezone.get_default_timezone()


def combine_local(date_value, time_value):
    """A local date + local time -> an aware datetime in the member's zone."""
    naive = datetime.combine(date_value, time_value)
    return timezone.make_aware(naive, local_tz())


def day_appointments(vendor, date_value, *, exclude_order=None):
    """This vendor's other visits on a local date, earliest first.

    Shown to the vendor before they commit, because "is 9am free?" is a question
    they should not have to answer from memory -- and the same query is what
    detects an overlap, so the list and the refusal can never disagree.
    """
    start = combine_local(date_value, datetime.min.time())
    end = start + timedelta(days=1)
    qs = (
        DispatchVisit.objects
        .filter(
            dispatch_order__vendor=vendor,
            scheduled_for__gte=start,
            scheduled_for__lt=end,
            completed_at__isnull=True,
        )
        .select_related("dispatch_order", "dispatch_order__client")
        .order_by("scheduled_for")
    )
    if exclude_order is not None:
        qs = qs.exclude(dispatch_order=exclude_order)
    return qs


def find_overlap(vendor, starts_at, ends_at, *, exclude_order=None):
    """The first existing visit this one would collide with, or None.

    Half-open intervals: a visit ending at 10:00 does NOT clash with one starting
    at 10:00. Treating touching appointments as a conflict would block the
    back-to-back bookings that make a day's route workable.
    """
    return (
        DispatchVisit.objects
        .filter(
            dispatch_order__vendor=vendor,
            completed_at__isnull=True,
            scheduled_for__lt=ends_at,
            scheduled_end__gt=starts_at,
        )
        .exclude(dispatch_order=exclude_order)
        .select_related("dispatch_order", "dispatch_order__client")
        .order_by("scheduled_for")
        .first()
    )


def _cancel_pending_reminders(visit):
    """Stand down the unsent reminders for a visit.

    Cancelled rather than deleted, so the record shows a reminder for the old slot
    existed and was stood down -- which is the difference between "we never set one"
    and "we moved the appointment".
    """
    DispatchReminder.objects.filter(
        visit=visit, sent_at__isnull=True, cancelled_at__isnull=True,
    ).update(cancelled_at=timezone.now())


def _create_reminders(visit):
    """One reminder per kind, for the vendor.

    The member gets the appointment text and their own day-before reminder; the
    vendor is the audience here because they are the one who has to travel.

    A reminder whose time has ALREADY PASSED is not created -- booking something for
    this afternoon should not fire a "day before" notice immediately, which is
    noise that teaches people to ignore reminders.
    """
    now = timezone.now()
    created = []
    for kind, offset in REMINDER_OFFSETS.items():
        send_at = visit.scheduled_for - offset
        if send_at <= now:
            continue
        created.append(DispatchReminder(
            visit=visit, kind=kind,
            audience=ReminderAudience.VENDOR, send_at=send_at,
        ))
    # The member's day-before reminder, same rule.
    member_send_at = visit.scheduled_for - REMINDER_OFFSETS[ReminderKind.DAY_BEFORE]
    if member_send_at > now:
        created.append(DispatchReminder(
            visit=visit, kind=ReminderKind.DAY_BEFORE,
            audience=ReminderAudience.MEMBER, send_at=member_send_at,
        ))
    if created:
        DispatchReminder.objects.bulk_create(created)
    return created


def _format_window(starts_at, ends_at):
    """"9:00 AM – 10:00 AM ET", in the member's zone."""
    tz = local_tz()
    start = starts_at.astimezone(tz)
    end = ends_at.astimezone(tz)
    fmt = "%-I:%M %p"
    return f"{start.strftime(fmt)} – {end.strftime(fmt)} ET"


def _format_when(starts_at):
    tz = local_tz()
    return starts_at.astimezone(tz).strftime("%A, %B %-d")


@transaction.atomic
def schedule_visit(
    order, *, date_value, arrival_time, duration_minutes=DEFAULT_DURATION,
    vendor_user=None, notes="",
):
    """Book (or move) the visit for an order, notify the member, set reminders.

    Returns ``(visit, message)`` where ``message`` is the member's SMS row -- which
    exists even when we could not send it, so the caller can tell the vendor
    whether the member was actually told.

    One visit per order, updated in place on a reschedule. A second row would make
    "when is this appointment?" ambiguous, and the StageEvent history already
    records that it moved.
    """
    from . import dispatch as dispatch_svc
    from . import messaging

    if duration_minutes not in ALLOWED_DURATIONS:
        raise SchedulingError(
            "bad_duration",
            f"Session length must be one of {', '.join(map(str, ALLOWED_DURATIONS))} minutes.",
        )
    if order.status in (DispatchStatus.UPLOADED, DispatchStatus.CANCELLED):
        raise SchedulingError(
            "order_closed", "This order is closed and cannot be scheduled.",
        )

    starts_at = combine_local(date_value, arrival_time)
    ends_at = starts_at + timedelta(minutes=duration_minutes)

    # Not refused, but worth knowing: booking into the past is almost always a
    # typo, and the vendor should be the one to decide.
    if starts_at < timezone.now() - timedelta(hours=1):
        raise SchedulingError(
            "in_the_past", "That time has already passed. Pick a future time.",
        )

    clash = find_overlap(
        order.vendor, starts_at, ends_at, exclude_order=order,
    )
    if clash is not None:
        raise SchedulingError(
            "overlap",
            "This time overlaps one of your existing appointments. "
            "Pick a different time.",
        )

    visit = DispatchVisit.objects.filter(dispatch_order=order).first()
    rescheduled = visit is not None and visit.scheduled_for is not None
    previous = visit.scheduled_for if rescheduled else None

    if visit is None:
        visit = DispatchVisit(dispatch_order=order)
    visit.scheduled_for = starts_at
    visit.scheduled_end = ends_at
    visit.confirmed_at = timezone.now()
    if notes:
        visit.notes = notes
    visit.save()

    _cancel_pending_reminders(visit)
    _create_reminders(visit)

    # CONFIRMED is what "the vendor has committed to a time" means. Recorded through
    # the shared service so the CRM's history and the vendor's action are the same
    # event rather than two accounts of it.
    dispatch_svc.set_status(
        order, DispatchStatus.CONFIRMED,
        source=StageEventSource.MANUAL,
        note=("visit rescheduled" if rescheduled else "visit scheduled"),
        metadata={
            "scheduled_for": starts_at.isoformat(),
            "duration_minutes": duration_minutes,
            "previous_scheduled_for": previous.isoformat() if previous else "",
            "vendor_user": getattr(vendor_user, "email", ""),
        },
    )

    kind = (
        MessageKind.APPOINTMENT_CHANGED if rescheduled
        else MessageKind.APPOINTMENT_SCHEDULED
    )
    phone, _phone_type, _notes = order.service_contact
    body = messaging.TEMPLATES[kind].format(
        first_name=(order.client.first_name or "").strip().title() or "there",
        when=_format_when(starts_at),
        window=_format_window(starts_at, ends_at),
        vendor=order.vendor.name if order.vendor_id else "our contractor",
    )
    message = messaging.send_to_member(
        order.client, body, kind=kind, to_number=phone,
        dispatch_order=order, vendor_user=vendor_user,
    )
    return visit, message


def due_reminders(now=None):
    """Reminders whose time has come and which have not been sent or cancelled."""
    now = now or timezone.now()
    return (
        DispatchReminder.objects
        .filter(send_at__lte=now, sent_at__isnull=True, cancelled_at__isnull=True)
        .select_related(
            "visit", "visit__dispatch_order", "visit__dispatch_order__client",
            "visit__dispatch_order__vendor",
        )
        .order_by("send_at")
    )


def fire_reminder(reminder):
    """Send one reminder and mark it sent.

    A MEMBER reminder is an SMS. A VENDOR reminder has nowhere to go yet -- the app
    has no push channel on iOS -- so it is marked sent and logged, which is honest:
    the vendor's own app shows the appointment, and inventing a delivery we cannot
    make would put a lie in the record.
    """
    from . import messaging

    order = reminder.visit.dispatch_order
    starts_at = reminder.visit.scheduled_for
    when_relative = (
        "tomorrow" if reminder.kind == ReminderKind.DAY_BEFORE else "in 30 minutes"
    )

    if reminder.audience == ReminderAudience.MEMBER:
        phone, _t, _n = order.service_contact
        body = messaging.TEMPLATES[MessageKind.APPOINTMENT_REMINDER].format(
            when_relative=when_relative,
            when=_format_window(starts_at, reminder.visit.scheduled_end),
            vendor=order.vendor.name if order.vendor_id else "our contractor",
        )
        messaging.send_to_member(
            order.client, body, kind=MessageKind.APPOINTMENT_REMINDER,
            to_number=phone, dispatch_order=order,
        )
    else:
        logger.info(
            "vendor reminder due: order=%s vendor=%s at %s (%s)",
            order.pk, order.vendor_id, starts_at, reminder.kind,
        )

    reminder.sent_at = timezone.now()
    reminder.save(update_fields=["sent_at"])
    return reminder
