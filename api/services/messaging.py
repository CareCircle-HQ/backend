"""Member SMS: store every message, then try to send.

TWILIO IS NOT WIRED UP. ``_deliver`` is the single placeholder, and it is the only
function that needs to change when credentials arrive. Everything else -- the
consent gate, the record, the audit fields, the member's message log -- is real
today, which is deliberate: the requirement is that all communication with a member
is stored, and that is worth having in place before the first real text goes out.

The same TODO exists in ``api/views_member_app.py`` for 2FA codes; both should be
switched over together.

DESIGN: the row is written BEFORE any send is attempted, and written even when we
decline to send. A log containing only successes cannot answer "did anyone actually
tell her?", which is the question it exists for.
"""
import logging

from django.conf import settings
from django.db.models import CharField, F, Func, Value
from django.utils import timezone

from ..models import (
    MemberMessage, MessageDirection, MessageKind, MessageStatus,
)

logger = logging.getLogger(__name__)

# Kept in one place so a member's log reads consistently and a wording change does
# not have to be hunted through view code. Times are always rendered in the
# member's own timezone by the caller -- never UTC, which would be meaningless to
# someone waiting at home.
TEMPLATES = {
    MessageKind.APPOINTMENT_SCHEDULED: (
        "Hi {first_name}, this is Met Council CareCircle. Your home assessment is "
        "booked for {when} with {vendor}. They will arrive between {window}. "
        "Reply to this message if you need to change it."
    ),
    MessageKind.APPOINTMENT_CHANGED: (
        "Hi {first_name}, your home assessment has been moved to {when} with "
        "{vendor}, arriving between {window}. Reply if that does not work."
    ),
    MessageKind.APPOINTMENT_REMINDER: (
        "Reminder: your home assessment is {when_relative} ({when}) with {vendor}. "
        "Reply if you need to change it."
    ),
}


def _deliver(message):
    """Hand a stored message to the SMS provider.

    TODO(twilio): replace the body of this function with a Twilio call --

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        sent = client.messages.create(
            to=message.to_number,
            from_=settings.TWILIO_FROM_NUMBER,
            body=message.body,
            status_callback=<the delivery-receipt webhook>,
        )
        message.provider = "twilio"
        message.provider_message_id = sent.sid
        message.status = MessageStatus.SENT
        message.sent_at = timezone.now()

    Until then the message stays QUEUED. Deliberately NOT marked "sent": claiming
    a text went out when no provider exists would make the log lie, and the log is
    the whole point. A real send later can pick these up, because QUEUED is exactly
    what a message looks like in the instant before delivery.
    """
    if not getattr(settings, "TWILIO_ACCOUNT_SID", ""):
        logger.info(
            "SMS not sent (no provider configured): to=%s kind=%s message=%s",
            message.to_number, message.kind, message.message_id,
        )
        return message
    raise NotImplementedError(
        "TWILIO_ACCOUNT_SID is set but the Twilio client is not implemented yet."
    )


def send_to_member(
    client, body, *, kind=MessageKind.FREEFORM, to_number="",
    dispatch_order=None, vendor_user=None, agent=None, require_consent=True,
):
    """Record a message to a member and attempt delivery. Returns the row.

    ``require_consent`` exists because not every message is a courtesy: an
    appointment confirmation is arguably operational. It defaults to True, and the
    caller has to say otherwise -- the consent the wizard collects should not be
    bypassed by accident.

    NEVER raises for a business reason. A member with no phone or no consent still
    produces a BLOCKED row, because the alternative is that scheduling an
    appointment fails over a text message, and the appointment matters more.
    """
    number = (to_number or "").strip()
    consent_ok = True
    blocked_reason = ""

    if require_consent and dispatch_order is not None:
        # service_consent, NOT consent_to_text: a work order inherits consent from
        # its assessment, where the wizard actually recorded it.
        _call_ok, consent_ok = dispatch_order.service_consent
        if not consent_ok:
            blocked_reason = "no_text_consent"
    if not number:
        blocked_reason = blocked_reason or "no_phone_number"

    message = MemberMessage.objects.create(
        client=client,
        direction=MessageDirection.OUTBOUND,
        kind=kind,
        to_number=number,
        body=body,
        dispatch_order=dispatch_order,
        sent_by_vendor_user=vendor_user,
        sent_by_agent=agent,
        status=MessageStatus.BLOCKED if blocked_reason else MessageStatus.QUEUED,
        error_code=blocked_reason,
        error_detail=(
            "The member has not consented to text messages."
            if blocked_reason == "no_text_consent"
            else "No mobile number on the order."
            if blocked_reason == "no_phone_number" else ""
        ),
    )
    if blocked_reason:
        logger.info(
            "SMS blocked (%s): client=%s kind=%s",
            blocked_reason, getattr(client, "pk", None), kind,
        )
        return message
    return _deliver(message)


def record_inbound(from_number, body, *, provider_message_id="", to_number=""):
    """Store an inbound text.

    Matched to a member by phone number, and stored UNMATCHED when no member is
    found rather than discarded -- an unmatched reply is the only evidence that
    someone tried to reach us, and throwing it away is the one unrecoverable
    option.

    Matching is best-effort on the last 10 digits, because numbers reach us in
    several formats and a member's stored number rarely matches E.164 exactly.
    """
    from ..models import Client

    digits = "".join(c for c in (from_number or "") if c.isdigit())[-10:]
    client = None
    if digits:
        # Numbers are STORED FORMATTED -- "(347) 555-0199" -- so a digits-only
        # __contains never matches. Strip the punctuation in SQL and compare the
        # last ten digits, which also makes "+1 347..." match "(347)...".
        #
        # client_phone_number is the ONLY phone field on Client; there is no
        # mobile_number, which an earlier draft assumed and which would have
        # raised FieldError on the first real inbound text.
        #
        # NB this is an expression, so it cannot use an index. Fine at SMS volumes;
        # if inbound traffic ever grows, add a functional index on the same
        # expression rather than caching the result somewhere it can go stale.
        client = (
            Client.objects
            .annotate(
                _digits=Func(
                    F("client_phone_number"),
                    Value(r"[^0-9]"), Value(""), Value("g"),
                    function="REGEXP_REPLACE",
                    output_field=CharField(),
                ),
            )
            .filter(_digits__endswith=digits)
            .order_by("client_added_at").first()
        )

    return MemberMessage.objects.create(
        client=client,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.INBOUND_REPLY,
        status=MessageStatus.RECEIVED,
        from_number=(from_number or "").strip(),
        to_number=(to_number or "").strip(),
        body=body or "",
        provider="twilio" if provider_message_id else "",
        provider_message_id=provider_message_id or "",
    )
