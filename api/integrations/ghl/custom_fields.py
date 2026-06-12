"""Mapping of GoHighLevel contact custom fields to ``Client`` data.

Each entry maps a GHL custom field id to a resolver that returns the value for a
given client (or ``None``/"" to skip). Add fields here incrementally -- the ids
come from ``python manage.py ghl_fields`` (raw dump in ghl_custom_fields.json).

Kept separate from contacts.py so the field catalog is easy to read, extend, and
remove when the external CRM is retired.
"""

# GHL custom field ids (location: HwYldKwhYiZywGFkXr0y).
FIELD_ENROLLMENT_CLIENT_ID = "iWw4cIFFBCFcKGlUeffm"  # contact.enrollment_client_id
FIELD_TOTAL_HOUSEHOLD_MEMBERS = "7Y8aipXfNywpogNFsEk0"  # contact.total_household_members

# Doctor/PCP fields
FIELD_DOCTORS_NAME = "VDp9dccvMPl8Yood6e9O"
FIELD_DOCTORS_STREET_ADDRESS = "Yvfn5jNSITA7oDZ9qc0G"
FIELD_DOCTORS_PHONE = "XtwIwYRKfgaTe88T92wB"
FIELD_DOCTORS_FAX = "XJyU9CjxrH5dID7vWtcC"
FIELD_DOCTORS_EMAIL = "ViDnbjtmh5VhDJHby2hW"

# E-Form fields (contact.*)
FIELD_IS_FAMILY = "din5iUlKVkx3YIbnNaUv"  # contact.family_client (Yes/No)
FIELD_TOTAL_FAMILY_MEMBERS = "OPaGDt1dA1vF4qFdTwbL"  # contact.family_members (numeric)
FIELD_ATTESTATION_NEEDED = "5kXJXWb2194QksUCp8dx"  # contact.attestation_needed (Yes/No)
FIELD_PREF_COMM_CHANNEL = "boUSF4vZkufTCeA4kbxT"  # contact.preferred_communication_channel
FIELD_PREF_COMM_LANGUAGE = "Txr8pNQPSw3tLIYOgy1L"  # contact.preferred_communication_language
FIELD_PREF_COMM_TIME = "yu7ThsTnI9PYyroLBhZR"  # contact.preferred_communication_time_of_day
FIELD_CALL_TRANSFER_ANSWERED = "EsUDKdpLn9hF4FQlufiJ"  # contact.transfer_answered

# Maps from stored Client codes back to the GHL picklist labels.
_CHANNEL_LABELS = {"phone": "Phone", "text": "SMS", "email": "Email"}
_TIME_LABELS = {
    "morning": "Morning (9am - 12pm)",
    "early_afternoon": "Early Afternoon (12pm - 3pm)",
    "late_afternoon": "Late Afternoon (3pm - 6pm)",
    "evening": "Evening (6pm - 8pm)",
}
_TRANSFER_LABELS = {
    "transfer_successful": "Transfer Successful (Verification Agent Answered)",
    "transfer_failed": "Transfer Failed (No Answer)",
    "no_verification_needed": "No Verification Needed",
}


def _enrollment_client_id(client):
    return str(client.client_id)


def _total_household_members(client):
    return client.total_family_members or client.household_size


def _doctors_name(client):
    return client.doctor_name


def _doctors_street_address(client):
    return client.doctor_street


def _doctors_phone(client):
    return client.doctor_phone


def _doctors_fax(client):
    return client.doctor_fax


def _doctors_email(client):
    return client.doctor_email


def _is_family(client):
    return "Yes" if client.is_a_family else "No"


def _total_family_members(client):
    return client.total_family_members


def _attestation_needed(client):
    return "Yes" if client.attestation_needed else "No"


def _pref_comm_channel(client):
    return [_CHANNEL_LABELS.get(c, c) for c in (client.communication_channels or []) if c]


def _pref_comm_time(client):
    return [_TIME_LABELS.get(t, t) for t in (client.preferred_communication_times or []) if t]


def _pref_comm_language(client):
    langs = client.preferred_languages or []
    return langs[0] if langs else None


def _call_transfer_answered(client):
    return _TRANSFER_LABELS.get(client.call_transfer_answered, "")


# (field_id, resolver) pairs. Only non-empty resolved values are sent.
# Extend this list as we confirm each field's mapping/options.
CONTACT_FIELD_RESOLVERS = [
    (FIELD_ENROLLMENT_CLIENT_ID, _enrollment_client_id),
    (FIELD_TOTAL_HOUSEHOLD_MEMBERS, _total_household_members),
    (FIELD_DOCTORS_NAME, _doctors_name),
    (FIELD_DOCTORS_STREET_ADDRESS, _doctors_street_address),
    (FIELD_DOCTORS_PHONE, _doctors_phone),
    (FIELD_DOCTORS_FAX, _doctors_fax),
    (FIELD_DOCTORS_EMAIL, _doctors_email),
    (FIELD_IS_FAMILY, _is_family),
    (FIELD_TOTAL_FAMILY_MEMBERS, _total_family_members),
    (FIELD_ATTESTATION_NEEDED, _attestation_needed),
    (FIELD_PREF_COMM_CHANNEL, _pref_comm_channel),
    (FIELD_PREF_COMM_TIME, _pref_comm_time),
    (FIELD_PREF_COMM_LANGUAGE, _pref_comm_language),
    (FIELD_CALL_TRANSFER_ANSWERED, _call_transfer_answered),
]


def build_custom_fields(client):
    """Return the GHL ``customFields`` array for a client (skips empties)."""
    out = []
    for field_id, resolver in CONTACT_FIELD_RESOLVERS:
        try:
            value = resolver(client)
        except Exception:
            value = None
        # Skip None, empty strings, and empty lists.
        if value is None or value == "" or value == []:
            continue
        out.append({"id": field_id, "field_value": value})
    return out
