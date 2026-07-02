import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from api.history import tracked_history
from api.fields import EncryptedTextField


# ---------------------------------------------------------------------------
# Enumerations (TextChoices)
# ---------------------------------------------------------------------------
class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"
    NONBINARY = "nonbinary", "Non-binary"
    TRANSGENDER = "transgender", "Transgender"
    OTHER = "other", "Other"
    DECLINED = "declined", "Declined to answer"
    UNKNOWN = "unknown", "Unknown"


class MaritalStatus(models.TextChoices):
    SINGLE = "single", "Single"
    MARRIED = "married", "Married"
    PARTNERED = "partnered", "Partnered"
    SEPARATED = "separated", "Separated"
    DIVORCED = "divorced", "Divorced"
    WIDOWED = "widowed", "Widowed"
    UNKNOWN = "unknown", "Unknown"


class ConsentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


class ClientStage(models.TextChoices):
    """Lifecycle stage for a client/person, maintained by
    ``api.services.lifecycle.recompute_client_stage``.

    The early funnel is *derived* from synced Unite Us data (consent,
    screenings, assessments, cases). Once an EnrollmentVerification exists, the
    later stages are *driven by* that enrollment's stage and take precedence.

    inactive -> consent -> screened -> assessment -> navigation ->
    pending_verification -> verified -> kitchen_assignment -> active ->
    completed   (terminal off-ramp: not_eligible)

    Verification is a yes/no fact (pop-up completed). The case authorization
    status (approved/pending/denied/expired) is a SEPARATE dimension on the Case
    -- it gates the move to kitchen_assignment but is never a lifecycle stage.
    """

    # --- Early funnel (derived from synced data) ---
    INACTIVE = "inactive", "Inactive"  # default: no consent / not pursued / disposed
    CONSENT = "consent", "Consent"  # consent accepted
    SCREENED = "screened", "Screened"  # >=1 completed Met Council screening
    ASSESSMENT = "assessment", "Assessment"  # completed assessment, eligible
    NAVIGATION = "navigation", "Navigation"  # >=1 Met Council case
    # --- Enrollment-driven (mirror EnrollmentVerification.stage) ---
    PENDING_VERIFICATION = "pending_verification", "Pending Verification"
    VERIFIED = "verified", "Verified"
    KITCHEN_ASSIGNMENT = "kitchen_assignment", "Kitchen Assignment"  # approved auth, awaiting manual kitchen assignment
    ACTIVE = "active", "Active"  # receiving deliveries
    COMPLETED = "completed", "Completed"  # after last delivery
    # --- Terminal off-ramp ---
    NOT_ELIGIBLE = "not_eligible", "Not Eligible"  # ineligible / closed without service


class CommunicationChannel(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Phone"
    TEXT = "text", "Text"
    MAIL = "mail", "Mail"


class PhoneType(models.TextChoices):
    MOBILE = "mobile", "Mobile"
    HOME = "home", "Home"
    WORK = "work", "Work"


class AddressType(models.TextChoices):
    CURRENT = "current", "Current"
    HOME = "home", "Home"
    WORK = "work", "Work"
    MAILING = "mailing", "Mailing"
    DELIVERY = "delivery", "Delivery"
    TEMPORARY = "temporary", "Temporary"


class USState(models.TextChoices):
    AL = "AL", "Alabama"
    AK = "AK", "Alaska"
    AZ = "AZ", "Arizona"
    AR = "AR", "Arkansas"
    CA = "CA", "California"
    CO = "CO", "Colorado"
    CT = "CT", "Connecticut"
    DE = "DE", "Delaware"
    DC = "DC", "District of Columbia"
    FL = "FL", "Florida"
    GA = "GA", "Georgia"
    HI = "HI", "Hawaii"
    ID = "ID", "Idaho"
    IL = "IL", "Illinois"
    IN = "IN", "Indiana"
    IA = "IA", "Iowa"
    KS = "KS", "Kansas"
    KY = "KY", "Kentucky"
    LA = "LA", "Louisiana"
    ME = "ME", "Maine"
    MD = "MD", "Maryland"
    MA = "MA", "Massachusetts"
    MI = "MI", "Michigan"
    MN = "MN", "Minnesota"
    MS = "MS", "Mississippi"
    MO = "MO", "Missouri"
    MT = "MT", "Montana"
    NE = "NE", "Nebraska"
    NV = "NV", "Nevada"
    NH = "NH", "New Hampshire"
    NJ = "NJ", "New Jersey"
    NM = "NM", "New Mexico"
    NY = "NY", "New York"
    NC = "NC", "North Carolina"
    ND = "ND", "North Dakota"
    OH = "OH", "Ohio"
    OK = "OK", "Oklahoma"
    OR = "OR", "Oregon"
    PA = "PA", "Pennsylvania"
    RI = "RI", "Rhode Island"
    SC = "SC", "South Carolina"
    SD = "SD", "South Dakota"
    TN = "TN", "Tennessee"
    TX = "TX", "Texas"
    UT = "UT", "Utah"
    VT = "VT", "Vermont"
    VA = "VA", "Virginia"
    WA = "WA", "Washington"
    WV = "WV", "West Virginia"
    WI = "WI", "Wisconsin"
    WY = "WY", "Wyoming"


class MilitaryAffiliation(models.TextChoices):
    SERVICE_MEMBER = "service_member", "Service Member"
    VETERAN = "veteran", "Veteran"
    FAMILY_MEMBER = "family_member", "Family Member of Service Member/Veteran"
    NONE = "none", "No Military Affiliation"
    DECLINED = "declined", "Declined to answer"


class MilitaryBranch(models.TextChoices):
    ARMY = "army", "Army"
    NAVY = "navy", "Navy"
    AIR_FORCE = "air_force", "Air Force"
    MARINES = "marines", "Marine Corps"
    COAST_GUARD = "coast_guard", "Coast Guard"
    SPACE_FORCE = "space_force", "Space Force"
    NATIONAL_GUARD = "national_guard", "National Guard"
    RESERVES = "reserves", "Reserves"


class DischargeType(models.TextChoices):
    HONORABLE = "honorable", "Honorable"
    GENERAL = "general", "General (Under Honorable Conditions)"
    OTHER_THAN_HONORABLE = "other_than_honorable", "Other Than Honorable"
    BAD_CONDUCT = "bad_conduct", "Bad Conduct"
    DISHONORABLE = "dishonorable", "Dishonorable"
    UNCHARACTERIZED = "uncharacterized", "Uncharacterized"


class InsurancePlanType(models.TextChoices):
    MEDICAID = "medicaid", "Medicaid"
    MEDICARE = "medicare", "Medicare"
    COMMERCIAL = "commercial", "Commercial"
    MARKETPLACE = "marketplace", "Marketplace"
    DUAL = "dual", "Dual (Medicare/Medicaid)"
    SELF_PAY = "self_pay", "Self Pay"
    OTHER = "other", "Other"


class RecordStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PENDING = "pending", "Pending"
    INACTIVE = "inactive", "Inactive"
    EXPIRED = "expired", "Expired"


class SocialCareCoverageStatus(models.TextChoices):
    """Enrollment status for a social care coverage plan (distinct from medical
    insurance, which uses RecordStatus)."""

    ENROLLED = "enrolled", "Enrolled"
    NON_ENROLLED = "non_enrolled", "Non-Enrolled"
    EXPIRED = "expired", "Expired"


class ServiceType(models.TextChoices):
    """Services a client may be eligible for / referred for (multi-select)."""

    COOKING_SUPPLIES = "cooking_supplies", "Cooking Supplies"
    SOW_DEVELOPMENT = "sow_development", "SOW Development"
    FRESH_PRODUCE_GROCERIES = (
        "fresh_produce_groceries",
        "Fresh Produce and Nonperishable Groceries",
    )
    HOME_ACCESSIBILITY = "home_accessibility", "Home Accessibility"
    HOME_REMEDIATION = "home_remediation", "Home Remediation"
    MTNA_FOOD_RX_BOXES = "mtna_food_rx_boxes", "MTNA Food Prescription Boxes"
    MTNA_FOOD_RX_VOUCHER = "mtna_food_rx_voucher", "MTNA Food Prescriptions Voucher"
    NUTRITIONAL_COUNSELING = (
        "nutritional_counseling_education",
        "Nutritional Counseling and Education",
    )
    REAUTHORIZATION = "reauthorization", "Reauthorization"
    CLINICALLY_APPROPRIATE_MEALS = (
        "clinically_appropriate_meals",
        "Clinically Appropriate Meals",
    )
    FOOD_PANTRY = "food_pantry", "Food Pantry"
    GROCERIES_TO_GO = "groceries_to_go", "Groceries to Go"
    HEALTH_HOME_ADULT_CARE = "health_home_adult_care", "Health Home Adult Care"
    HOUSING_TRANSITION = "housing_transition", "Housing Transition"
    MEDICALLY_TAILORED_MEALS = "medically_tailored_meals", "Medically Tailored Meals (MTM)"
    OTHER = "other", "Other"
    SNAP = "snap", "SNAP"
    TENANCY = "tenancy", "Tenancy"
    TRANSPORTATION = "transportation", "Transportation"
    NONE = "none", "None"


class CommunicationTimeOfDay(models.TextChoices):
    MORNING = "morning", "Morning (9am - 12pm)"
    EARLY_AFTERNOON = "early_afternoon", "Early Afternoon (12pm - 3pm)"
    LATE_AFTERNOON = "late_afternoon", "Late Afternoon (3pm - 6pm)"
    EVENING = "evening", "Evening (6pm - 8pm)"


class CallTransferStatus(models.TextChoices):
    TRANSFER_SUCCESSFUL = (
        "transfer_successful",
        "Transfer Successful (Verification Agent Answered)",
    )
    TRANSFER_FAILED = "transfer_failed", "Transfer Failed (No Answer)"
    NO_VERIFICATION_NEEDED = "no_verification_needed", "No Verification Needed"


class ClientLevel(models.TextChoices):
    """Service level a client falls under, derived from the "Client May Be
    Eligible For" services whose names carry a "Level 1"/"Level 2" marker."""

    LEVEL_1 = "level_1", "Level 1"
    LEVEL_2 = "level_2", "Level 2"


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def default_communication_time_of_day():
    """Per-day preferred contact windows. Each day holds a list of values from
    CommunicationTimeOfDay (e.g. ["morning", "evening"])."""
    return {day: [] for day in WEEKDAYS}


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------
class Client(models.Model):
    """A client/person ingested from the source system (Unite Us)."""

    # --- Core Identification ---
    client_id = models.UUIDField(primary_key=True, editable=False)
    first_name = models.CharField(max_length=120)
    middle_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120)
    date_of_birth = models.DateField(null=True, blank=True)  # PII
    client_phone_number = models.CharField(max_length=30, blank=True)  # PII
    phone_type = models.CharField(max_length=20, blank=True)  # mobile/home/work
    client_email_address = models.EmailField(blank=True)  # PII
    consent_accepted = models.BooleanField(default=False)
    consent_status = models.CharField(max_length=20, blank=True)  # E-form: accepted/declined
    consented_at = models.DateTimeField(null=True, blank=True)
    consent_doc_url = models.URLField(blank=True)

    # --- Lifecycle funnel (maintained by api.services.lifecycle) ---
    lifecycle_stage = models.CharField(
        max_length=25, choices=ClientStage.choices,
        default=ClientStage.INACTIVE, db_index=True,
    )
    lifecycle_stage_at = models.DateTimeField(null=True, blank=True)

    # --- CRM Sync (External - GoHighLevel) ---
    crm_contact_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    # --- Metadata ---
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    # --- Demographics ---
    gender = models.CharField(max_length=20, blank=True)  # male/female/other/unknown
    marital_status = models.CharField(max_length=20, blank=True)  # single/married/divorced/widowed
    race = models.CharField(max_length=80, blank=True)  # E-form
    ethnicity = models.CharField(max_length=80, blank=True)  # E-form
    sexuality = models.CharField(max_length=80, blank=True)  # E-form
    education = models.CharField(max_length=50, blank=True)  # high school, college, etc.
    language = models.CharField(max_length=50, blank=True)  # Primary language (English, Spanish, etc.)
    preferred_spoken_language = models.CharField(max_length=50, blank=True)  # E-form
    preferred_written_language = models.CharField(max_length=50, blank=True)  # E-form
    employment_status = models.CharField(max_length=1, blank=True)  # Enum
    citizenship = models.CharField(max_length=80, blank=True)  # Unite Us export
    household_size = models.PositiveSmallIntegerField(null=True, blank=True)
    adults_in_household = models.PositiveSmallIntegerField(null=True, blank=True)
    children_in_household = models.PositiveSmallIntegerField(null=True, blank=True)
    household_income_range = models.CharField(max_length=2, blank=True)  # Enum
    # Free-text monthly income from the Unite Us export (kept as a string so we
    # don't lose source fidelity, e.g. "$2,500" or "2500").
    gross_monthly_income = models.CharField(max_length=40, blank=True)
    # Assigned care coordinator (client-level) from the Unite Us export.
    care_coordinator = models.CharField(max_length=255, blank=True)
    care_coordinator_status = models.CharField(max_length=20, blank=True)  # active/inactive

    # --- Program Fields (from E-Form) ---
    screening_call_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    eligibility_call_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    cases_call_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    call_transfer_answered = models.CharField(
        max_length=30, choices=CallTransferStatus.choices, blank=True
    )
    preferred_contact_method = models.CharField(max_length=1, blank=True)  # Enum
    communication_channels = models.JSONField(default=list, blank=True)  # Array of codes
    preferred_communication_times = models.JSONField(default=list, blank=True)  # Array of codes
    preferred_languages = models.JSONField(default=list, blank=True)  # Array of codes
    agent_code = models.CharField(max_length=60, blank=True)
    agent_name = models.CharField(max_length=255, blank=True)
    lead_source = models.CharField(max_length=80, blank=True)
    # Williamsburg exception flag: derived from ``lead_source == "Williamsburg"``
    # (set on every client upsert). When set, an ext verification request is
    # fast-tracked straight to Service Active (see api.services.williamsburg)
    # instead of waiting on the manual verification + kitchen-assignment steps.
    is_williamsburg = models.BooleanField(default=False, db_index=True)
    is_a_family = models.BooleanField(default=False)
    total_family_members = models.PositiveSmallIntegerField(null=True, blank=True)
    attestation_needed = models.BooleanField(default=False)

    # --- Doctor Information (shown only if attestation_needed=True) ---
    doctor_name = models.CharField(max_length=255, blank=True)
    doctor_street = models.CharField(max_length=255, blank=True)
    doctor_city = models.CharField(max_length=120, blank=True)
    doctor_state = models.CharField(max_length=2, blank=True)
    doctor_zip = models.CharField(max_length=10, blank=True)
    doctor_phone = models.CharField(max_length=30, blank=True)
    doctor_fax = models.CharField(max_length=30, blank=True)
    doctor_email = models.EmailField(blank=True)

    # --- Eligibility & Referral ---
    elegible_programs = models.JSONField(default=list, blank=True)  # From screening
    referred_for = models.JSONField(default=list, blank=True)  # From eligibility
    # Derived on assessment save from eligible service names carrying a
    # "Level 1"/"Level 2" marker. Level 2 takes precedence over Level 1.
    is_level = models.CharField(
        max_length=10, choices=ClientLevel.choices, blank=True
    )

    history = tracked_history()

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["last_name", "first_name"]),
            models.Index(fields=["date_of_birth"]),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.client_id})"


class MilitaryProfile(models.Model):
    """Military/veteran details. One-to-one to keep Client lean (mostly null otherwise)."""

    client = models.OneToOneField(
        Client, on_delete=models.CASCADE, related_name="military_profile"
    )
    military_affiliation = models.CharField(
        max_length=20, choices=MilitaryAffiliation.choices, blank=True
    )
    military_entry_date = models.DateField(null=True, blank=True)
    military_exit_date = models.DateField(null=True, blank=True)
    current_status = models.CharField(max_length=80, blank=True)
    currently_transitioning = models.BooleanField(null=True, blank=True)
    at_least_one_day_active_duty = models.BooleanField(null=True, blank=True)
    deployed = models.BooleanField(null=True, blank=True)
    deployment_start_date = models.DateField(null=True, blank=True)
    deployment_end_date = models.DateField(null=True, blank=True)
    branch = models.CharField(
        max_length=20, choices=MilitaryBranch.choices, blank=True
    )
    service_era = models.CharField(max_length=80, blank=True)
    discharge_type = models.CharField(
        max_length=30, choices=DischargeType.choices, blank=True
    )
    discharged_due_to_disability = models.BooleanField(null=True, blank=True)
    service_connected_disability = models.BooleanField(null=True, blank=True)
    service_connected_disability_rating = models.PositiveSmallIntegerField(
        null=True, blank=True
    )  # 0-100
    proof_of_veteran_status = models.BooleanField(null=True, blank=True)
    proof_type = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return f"Military profile for {self.client_id}"


class Address(models.Model):
    """Normalized address. Supports current + delivery types per profile.md."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="addresses"
    )
    type = models.CharField(
        max_length=20, choices=AddressType.choices, default=AddressType.CURRENT
    )
    street = models.CharField(max_length=255, blank=True)  # PII
    # Unit / apartment / suite number (kept separate from street so the kitchen
    # and delivery label can show it distinctly).
    unit = models.CharField(max_length=60, blank=True)  # PII
    city = models.CharField(max_length=120, blank=True)
    county = models.CharField(max_length=120, blank=True)  # Unite Us export
    state = models.CharField(max_length=2, blank=True)
    zip = models.CharField(max_length=10, blank=True)
    # Free-text delivery instructions for this address (e.g. "Call on arrival,
    # leave at front desk"). Captured during verification; flows to the kitchen
    # on the delivery order.
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["type"]
        indexes = [
            models.Index(fields=["client", "type"]),
            models.Index(fields=["zip"]),
        ]

    def __str__(self):
        return f"{self.get_type_display()} address for {self.client_id}"


class ClientPhoneSource(models.TextChoices):
    UNITEUS = "uniteus", "Unite Us"
    CALLTOOLS = "calltools", "CallTools"
    AGENT = "agent", "Agent"


class ClientPhone(models.Model):
    """A phone number tied to a client. A client accumulates many numbers over
    time (the original Unite Us number plus any numbers they call in from), and
    the *same* number may be tied to multiple clients (e.g. household members
    sharing a phone). Matching is done on ``normalized`` (last-10 digits, the
    same convention as ``calltools.presence.numbers_match``)."""

    client_phone_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="phones"
    )
    raw = models.CharField(max_length=40)  # as received / entered
    normalized = models.CharField(max_length=20, db_index=True)  # last 10 digits
    label = models.CharField(max_length=20, blank=True)  # mobile/home/work
    source = models.CharField(
        max_length=20,
        choices=ClientPhoneSource.choices,
        default=ClientPhoneSource.AGENT,
    )
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_primary", "-created_at"]
        constraints = [
            # A client can't list the same number twice; two different clients
            # CAN share a number (no global uniqueness).
            models.UniqueConstraint(
                fields=["client", "normalized"],
                name="uniq_client_phone_normalized",
            )
        ]
        indexes = [
            models.Index(fields=["normalized"]),
            models.Index(fields=["client", "is_primary"]),
        ]

    @staticmethod
    def normalize(value):
        """Reduce a phone number to comparable digits (last 10, US-style).
        Mirrors ``api.integrations.calltools.presence._normalize_number``."""
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return digits[-10:] if len(digits) >= 10 else digits

    def save(self, *args, **kwargs):
        if not self.normalized:
            self.normalized = self.normalize(self.raw)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.raw} -> {self.client_id}"


class Insurance(models.Model):
    """Normalized insurance record. A client may have multiple plans over time."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="insurances"
    )
    plan_external_id = models.CharField(max_length=64, blank=True, db_index=True)
    plan_type = models.CharField(
        max_length=20, choices=InsurancePlanType.choices,
        default=InsurancePlanType.MEDICAID, blank=True,
    )
    plan_name = models.CharField(max_length=255, blank=True)
    insurance_id = models.CharField(max_length=64, blank=True, db_index=True)  # PII
    status = models.CharField(
        max_length=20, choices=RecordStatus.choices, blank=True
    )
    is_primary = models.BooleanField(default=False)
    external_group_id = models.CharField(max_length=64, blank=True)
    external_member_id = models.CharField(max_length=64, blank=True)  # PII
    ingested = models.BooleanField(default=False)
    enrolled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    record_status = models.CharField(
        max_length=20, choices=RecordStatus.choices, blank=True
    )
    verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    history = tracked_history()

    class Meta:
        ordering = ["-is_primary", "-enrolled_at"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["insurance_id"]),
        ]

    def __str__(self):
        return f"{self.plan_name or self.plan_type} for {self.client_id}"


class SocialCareCoverage(models.Model):
    """Normalized social care coverage record. Split out from Insurance so social
    coverage (Enrolled/Non-Enrolled/Expired) is tracked separately from medical
    insurance (Active/Inactive/Expired). A client may have multiple records over
    time. Default plan_type is Medicaid."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="social_care_coverages"
    )
    coverage_id = models.CharField(max_length=64, blank=True, db_index=True)  # source id
    plan_type = models.CharField(
        max_length=20, choices=InsurancePlanType.choices,
        default=InsurancePlanType.MEDICAID, blank=True,
    )
    plan_name = models.CharField(max_length=255, blank=True)
    external_member_id = models.CharField(max_length=64, blank=True)  # PII
    external_group_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=20, choices=SocialCareCoverageStatus.choices, blank=True
    )
    enrolled_at = models.DateTimeField(null=True, blank=True)  # Start Date
    expired_at = models.DateTimeField(null=True, blank=True)  # End Date
    verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    ingested = models.BooleanField(default=False)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    history = tracked_history()

    class Meta:
        ordering = ["-enrolled_at"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["external_member_id"]),
        ]

    def __str__(self):
        return f"{self.plan_name or 'Social Care Coverage'} for {self.client_id}"


# ===========================================================================
# PRODUCT & SERVICE CATALOG
# ===========================================================================
class Service(models.Model):
    """A service type a client may be eligible for, taken from the eligibility
    result ("Client May Be Eligible for:"). Mirrors the Unite Us taxonomy
    (``ServiceType``). We keep every value, but only flag the ones we offer
    (``is_offered``). Currently offered: Medically Tailored Meals (MTM) and
    Clinically Appropriate Meals.
    """

    code = models.CharField(
        max_length=80, choices=ServiceType.choices, unique=True
    )
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=120, blank=True)  # e.g. "Food"
    # Master-list link: a Program (from assessments/cases) can have many
    # service types. Set from Cases (service_type + program_name).
    program = models.ForeignKey(
        "Program",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_types",
    )
    # We provide this service (vs. merely tracking that it exists in Unite Us).
    is_offered = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_offered", "is_active"]),
        ]

    def __str__(self):
        return self.name


# ===========================================================================
# HOUSEHOLD DOMAIN
# ===========================================================================
class Household(models.Model):
    """A family/household group that ties multiple clients together.

    Members (including the primary) are stored as HouseholdMember rows, so the
    full group is ``household.members``. A client belongs to at most one
    household (enforced by the OneToOne on HouseholdMember.client)."""

    household_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True)  # optional label e.g. "Doe Household"
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Household {self.household_id}"


class HouseholdMember(models.Model):
    """Membership of a single client in a household (the intermediary model).

    The primary member is included here too, flagged ``is_primary`` (at most one
    primary per household)."""

    household = models.ForeignKey(
        Household, on_delete=models.CASCADE, related_name="members"
    )
    # OneToOne: a client can be in at most one household at a time.
    client = models.OneToOneField(
        Client, on_delete=models.CASCADE, related_name="household_membership"
    )
    is_primary = models.BooleanField(default=False)
    relationship = models.CharField(max_length=60, blank=True)  # e.g. spouse, child, guardian
    # Login username for the Benefully member mobile app. This is the mobile
    # number the member chooses to sign in with (also the destination for the
    # SMS 2FA code). Unique across members; null when the member hasn't enrolled
    # in the app yet.
    mobile_app_username = models.CharField(
        max_length=32, null=True, blank=True, unique=True, db_index=True
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_primary", "added_at"]
        constraints = [
            # At most one primary member per household.
            models.UniqueConstraint(
                fields=["household"],
                condition=models.Q(is_primary=True),
                name="unique_primary_per_household",
            ),
        ]

    def __str__(self):
        tag = " (primary)" if self.is_primary else ""
        return f"{self.client_id} in {self.household_id}{tag}"


# ===========================================================================
# CASE DOMAIN
# ===========================================================================
class CaseStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    PENDING_AUTHORIZATION = "pending_authorization", "Pending Authorization"
    MANAGED = "managed", "Managed"
    OFF_PLATFORM = "off_platform", "Off Platform"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Cancelled"


class OutcomeResolutionType(models.TextChoices):
    RESOLVED = "resolved", "Resolved"
    UNRESOLVED = "unresolved", "Unresolved"
    REFERRED_OUT = "referred_out", "Referred Out"
    NO_LONGER_NEEDED = "no_longer_needed", "No Longer Needed"
    UNABLE_TO_CONTACT = "unable_to_contact", "Unable to Contact"
    INELIGIBLE = "ineligible", "Ineligible"
    OTHER = "other", "Other"


class ServiceAuthorizationStatus(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not Required"
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    EXPIRED = "expired", "Expired"


class CaseType(models.TextChoices):
    """Classification of a case. Auto-derived on save by matching the case's
    program_name against the ProgramPipeline table (program_name -> case_category)
    and mapping that category here; falls back to the service_type heuristic
    (Social Service Case Management => Internal Service, else => Navigation) when
    the program is not found in ProgramPipeline."""

    NAVIGATION = "navigation", "Navigation"
    EXTERNAL_SERVICE = "external_service", "External Service"
    INTERNAL_SERVICE = "internal_service", "Internal Service"
    ELIGIBILITY = "eligibility", "Eligibility"


class CaseHouseholdType(models.TextChoices):
    """Whether a case is tracked for a single individual or a household.
    Auto-derived on save from the client's household data."""

    INDIVIDUAL = "individual", "Individual"
    HOUSEHOLD = "household", "Household"


class Provider(models.Model):
    """Normalized provider/organization from the source system."""

    provider_id = models.UUIDField(primary_key=True, editable=False)
    name = models.CharField(max_length=255)
    network_id = models.UUIDField(null=True, blank=True)
    network_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["network_id"])]

    def __str__(self):
        return self.name


class ProgramMainCategory(models.Model):
    """Master list of program main categories (e.g. Housing, Food, Social
    Support, Transportation). Built up from saved Screening results; Programs
    are related to a category later.
    """

    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "program main categories"

    def __str__(self):
        return self.name


class ProductTypeKind(models.TextChoices):
    """The kind of product an Internal Service program delivers."""

    MEALS = "meals", "Meals"
    BOXES = "boxes", "Boxes"


class DeliveryCadence(models.TextChoices):
    """How often a product type is delivered each week."""

    MON_THU = "mon_thu", "Mon/Thu"
    TUE_FRI = "tue_fri", "Tue/Fri"
    ONCE_A_WEEK = "once_a_week", "Once a Week"


class ProductType(models.Model):
    """A deliverable product (Meals or Boxes) with its per-delivery quantity and
    weekly delivery cadence. Programs that fulfill Internal Service cases are
    linked to one of these based on a keyword in the program name."""

    product_type_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    type = models.CharField(
        max_length=20, choices=ProductTypeKind.choices
    )
    prod_per_delivery = models.PositiveIntegerField(default=0)
    # For meals: the per-DAY meal rate (e.g. 3). The per-delivery quantity is
    # this rate multiplied by the number of days that delivery covers (the gap
    # to the next delivery in the cadence), so a Mon/Thu cadence yields 9 then
    # 12 meals. Unused for boxes (which use the flat ``prod_per_delivery``).
    meals_per_day = models.PositiveSmallIntegerField(default=0)
    delivery_days_cadence = models.CharField(
        max_length=20, choices=DeliveryCadence.choices, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["type", "delivery_days_cadence"]
        # The same product (e.g. Meals) may exist with different delivery
        # cadences, but each (type, cadence) pair must be unique.
        constraints = [
            models.UniqueConstraint(
                fields=["type", "delivery_days_cadence"],
                name="uniq_product_type_type_cadence",
            )
        ]

    def __str__(self):
        cadence = self.get_delivery_days_cadence_display()
        return f"{self.get_type_display()} ({cadence})" if cadence else self.get_type_display()


class Weekday(models.IntegerChoices):
    """Weekday numbering matching Python's ``date.weekday()`` (Mon == 0)."""

    MON = 0, "Monday"
    TUE = 1, "Tuesday"
    WED = 2, "Wednesday"
    THU = 3, "Thursday"
    FRI = 4, "Friday"
    SAT = 5, "Saturday"
    SUN = 6, "Sunday"


class CadenceRule(models.Model):
    """Editable rule that decides a member's delivery cadence from the weekday a
    case becomes active (authorization Accepted), per product kind.

    One row per (product_kind, accepted_weekday). Each row defines the cadence
    assigned, the weekdays deliveries land on, the weekdays purchase orders are
    generated, and the weekday of the *first* delivery (which lets us encode
    custom rules like "accepted Wednesday skips this week's Thursday and starts
    the following Monday").

    Weekday lists use the lowercase codes shared with
    ``EnrollmentVerification.delivery_weekdays`` ("mon", "tue", ... "sun").
    """

    WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    product_kind = models.CharField(
        max_length=20, choices=ProductTypeKind.choices
    )
    accepted_weekday = models.IntegerField(
        choices=Weekday.choices,
        help_text="Weekday the case authorization is accepted (becomes active).",
    )
    cadence = models.CharField(
        max_length=20, choices=DeliveryCadence.choices
    )
    delivery_weekdays = models.JSONField(
        default=list, blank=True,
        help_text='Weekdays deliveries land on, e.g. ["mon", "thu"].',
    )
    po_weekdays = models.JSONField(
        default=list, blank=True,
        help_text='Weekdays purchase orders are generated, e.g. ["tue", "fri"].',
    )
    first_delivery_weekday = models.IntegerField(
        choices=Weekday.choices,
        help_text="Weekday of the first delivery (the next delivery day after activation).",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product_kind", "accepted_weekday"]
        constraints = [
            models.UniqueConstraint(
                fields=["product_kind", "accepted_weekday"],
                name="uniq_cadence_rule_kind_weekday",
            )
        ]

    def __str__(self):
        return (
            f"{self.get_product_kind_display()} / accepted "
            f"{self.get_accepted_weekday_display()} -> {self.get_cadence_display()}"
        )


class Program(models.Model):
    """Normalized program offered by a provider.

    Operational rows come from Cases keyed by the source ``program_id`` UUID.
    The master list is also fed (deduped by ``name``) from Assessment
    "Client May Be Eligible" results and Case program names; those name-based
    rows get an auto-generated ``program_id``.
    """

    program_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )
    main_category = models.ForeignKey(
        ProgramMainCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )
    # Set for Internal Service programs: Meals vs Boxes, derived from the program
    # name. See api.services.catalog.assign_product_type_for_internal_service.
    product_type = models.ForeignKey(
        ProductType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )
    description = models.TextField(blank=True)
    # Optional rule-based eligibility criteria (machine-evaluable rules).
    eligibility_rules = models.JSONField(default=dict, blank=True)
    # Optional eligibility questions surfaced to the member (list of question
    # definitions).
    eligibility_questions = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProgramEligibility(models.Model):
    """A member's predicted eligibility for a Program (model-scored).

    One row per evaluation; the latest by ``evaluated_at`` reflects the current
    standing for a given ``model_version``.
    """

    member = models.ForeignKey(
        HouseholdMember,
        on_delete=models.CASCADE,
        related_name="program_eligibilities",
    )
    program = models.ForeignKey(
        Program,
        on_delete=models.CASCADE,
        related_name="eligibilities",
    )
    # Predicted probability in [0, 1].
    eligibility_score = models.FloatField(null=True, blank=True)
    # Thresholded decision derived from ``eligibility_score``.
    is_eligible = models.BooleanField(default=False)
    model_version = models.CharField(max_length=60, blank=True)
    evaluated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-evaluated_at"]
        indexes = [
            models.Index(fields=["member", "program"]),
            models.Index(fields=["program", "is_eligible"]),
        ]
        verbose_name_plural = "program eligibilities"

    def __str__(self):
        return f"{self.member_id} / {self.program_id}: {self.eligibility_score}"


class ProgramDisplayLog(models.Model):
    """Tracks a Program being shown to a member in the app and the member's
    downstream actions (click / apply) for engagement analytics."""

    member = models.ForeignKey(
        HouseholdMember,
        on_delete=models.CASCADE,
        related_name="program_display_logs",
    )
    program = models.ForeignKey(
        Program,
        on_delete=models.CASCADE,
        related_name="display_logs",
    )
    displayed_at = models.DateTimeField(auto_now_add=True)
    clicked = models.BooleanField(default=False)
    applied = models.BooleanField(default=False)

    class Meta:
        ordering = ["-displayed_at"]
        indexes = [
            models.Index(fields=["member", "program"]),
            models.Index(fields=["program", "displayed_at"]),
        ]

    def __str__(self):
        return f"{self.member_id} / {self.program_id} @ {self.displayed_at:%Y-%m-%d}"


class Case(models.Model):
    """A case opened for a client. A client may have many cases over time."""

    # --- Core Case Information ---
    case_id = models.UUIDField(primary_key=True, editable=False)  # source external_id
    subject_id = models.UUIDField(db_index=True, null=True, blank=True)  # source client reference
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="cases"
    )
    previous_case = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_cases",
    )
    created_by_id = models.UUIDField(null=True, blank=True)  # source agent id
    created_by_name = models.CharField(max_length=255, blank=True)
    date_opened = models.DateTimeField(null=True, blank=True)  # Date Opened from Unite Us
    updated_at = models.DateTimeField(null=True, blank=True)  # source last update

    # Product (model to be defined later) - placeholder reference for now.
    product_id = models.UUIDField(null=True, blank=True)

    # --- Case Dates & Timeline ---
    ar_submitted_on = models.DateTimeField(null=True, blank=True)
    case_processed_at = models.DateTimeField(null=True, blank=True)
    case_managed_at = models.DateTimeField(null=True, blank=True)
    case_off_platform_at = models.DateTimeField(null=True, blank=True)
    case_closed_at = models.DateTimeField(null=True, blank=True)

    # --- Case Status & Description ---
    case_description = models.TextField(blank=True)
    case_status = models.CharField(
        max_length=25, choices=CaseStatus.choices, default=CaseStatus.OPEN
    )
    closed_note = models.TextField(blank=True)
    started_as_assistance_request = models.BooleanField(default=False)
    case_is_referred = models.BooleanField(default=False)

    # --- Network & Provider Information ---
    network_id = models.UUIDField(null=True, blank=True)
    network_name = models.CharField(max_length=255, blank=True)
    originating_provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originated_cases",
    )
    originating_provider_name = models.CharField(max_length=255, blank=True)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="serviced_cases",
    )
    provider_name = models.CharField(max_length=255, blank=True)
    out_of_network_provider_name = models.CharField(max_length=255, blank=True)

    # --- Program Information ---
    program = models.ForeignKey(
        Program,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cases",
    )
    # service_type = Service Type from list view / Service Type from detail

    # --- Case Assignment ---
    primary_worker_id = models.UUIDField(null=True, blank=True)
    primary_worker_name = models.CharField(max_length=255, blank=True)
    agent_code = models.CharField(max_length=60, blank=True)  # Agent code from E-form / care coordinator

    # --- Service Information ---
    # Service Type from list view (what we called program_name before)
    service_type = models.CharField(max_length=255, blank=True, db_index=True)
    # Program Name from detail view (what we called service_subtype before)
    program_name = models.CharField(max_length=255, blank=True)

    # --- Case Classification (auto-derived on upsert) ---
    # Internal Service when service_type is a meal/box subtype (Medically
    # Tailored Meals / Produce Prescription/Voucher); otherwise the
    # program_name's ProgramPipeline category (Eligibility / Navigation /
    # External Service), defaulting to Navigation. See derive_case_type.
    case_type = models.CharField(
        max_length=20, choices=CaseType.choices, default=CaseType.INTERNAL_SERVICE
    )
    # Individual vs Household, derived from the client's household data.
    household_type = models.CharField(
        max_length=12,
        choices=CaseHouseholdType.choices,
        default=CaseHouseholdType.INDIVIDUAL,
    )

    # --- Outcome Information ---
    outcome_id = models.UUIDField(null=True, blank=True)
    outcome_description = models.TextField(blank=True)
    outcome_resolution_type = models.CharField(
        max_length=20, choices=OutcomeResolutionType.choices, blank=True
    )

    # --- Service Authorization ---
    service_authorization_status = models.CharField(
        max_length=20, choices=ServiceAuthorizationStatus.choices, blank=True
    )
    # Raw status label as shown in the UI (e.g. "Accepted") preserving fidelity
    # since the enum above normalizes it.
    service_authorization_status_label = models.CharField(max_length=80, blank=True)
    service_authorization_request_starts_at = models.DateTimeField(null=True, blank=True)
    service_authorization_request_ends_at = models.DateTimeField(null=True, blank=True)
    # Requested (pre-decision) dollar amount from the Unite Us authorization
    # (requested_cents). The approved figure lives in authorized_amount.
    service_authorization_requested_amount = models.CharField(max_length=120, blank=True)
    service_authorization_approval_starts_at = models.DateTimeField(null=True, blank=True)
    service_authorization_approval_ends_at = models.DateTimeField(null=True, blank=True)
    unite_us_authorization_id = models.CharField(max_length=80, blank=True)
    # Free text: usually a dollar amount (e.g. "$8,736.00") but can also be a
    # unit/time description (e.g. "20 units (293-307 minutes)").
    authorized_amount = models.CharField(max_length=120, blank=True)
    authorized_unit = models.CharField(max_length=80, blank=True)  # Unit from authorization
    authorized_rate = models.CharField(max_length=80, blank=True)  # Rate from authorization
    program_cap = models.TextField(blank=True)
    authorization_note = models.TextField(blank=True)

    # --- Social Care Coverage (as shown on the case) ---
    social_care_coverage_plan = models.CharField(max_length=255, blank=True)
    social_care_coverage_status = models.CharField(max_length=80, blank=True)

    # --- CRM Sync Tracking (External - GoHighLevel Opportunity) ---
    crm_opportunity_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    history = tracked_history()

    class Meta:
        ordering = ["-date_opened"]
        indexes = [
            models.Index(fields=["client", "case_status"]),
            models.Index(fields=["provider"]),
            models.Index(fields=["originating_provider"]),
            models.Index(fields=["program"]),
            models.Index(fields=["case_status"]),
            models.Index(fields=["created_by_id"]),
        ]

    def __str__(self):
        return f"Case {self.case_id} ({self.get_case_status_display()})"


class ContractedService(models.Model):
    """A contracted service (Unite Us ``provided_service``) on a case.

    A case may have one or more contracted services. Each maps to a Unite Us
    ``provided_service`` and carries its service-authorization terms (amount,
    delivery window, duration) plus the latest invoice (number + link). Keyed on
    the source ``provided_service`` id so imports are idempotent upserts.
    """

    # Source ``provided_service`` external id.
    contracted_service_id = models.UUIDField(primary_key=True, editable=False)
    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name="contracted_services"
    )

    # --- Service definition (fee_schedule_program) ---
    name = models.CharField(max_length=255, blank=True)
    service_type = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=80, blank=True)  # raw provided_service state
    fee_schedule_program_id = models.UUIDField(null=True, blank=True)
    fee_schedule_program_name = models.CharField(max_length=255, blank=True)
    unit_type = models.CharField(max_length=80, blank=True)

    # --- Service authorization terms ---
    service_authorization_id = models.UUIDField(null=True, blank=True)
    unite_us_authorization_id = models.CharField(max_length=80, blank=True)  # short_id
    authorization_status = models.CharField(max_length=80, blank=True)
    # Free text: dollar amount (e.g. "$8,736.00") or unit/time description.
    authorized_amount = models.CharField(max_length=120, blank=True)
    authorized_units = models.CharField(max_length=80, blank=True)
    # Free text duration, e.g. "20 units (293-307 minutes)".
    service_duration = models.CharField(max_length=255, blank=True)
    service_starts_at = models.DateField(null=True, blank=True)
    service_ends_at = models.DateField(null=True, blank=True)

    # --- Invoice (latest active invoice for this provided service) ---
    invoice_number = models.CharField(max_length=120, blank=True)
    invoice_status = models.CharField(max_length=80, blank=True)
    invoice_amount = models.CharField(max_length=120, blank=True)
    invoice_url = models.URLField(max_length=1000, blank=True)  # invoice link
    invoiced_at = models.DateTimeField(null=True, blank=True)

    # --- Source / ingest metadata ---
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    history = tracked_history()

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["case"]),
            models.Index(fields=["service_authorization_id"]),
        ]

    def __str__(self):
        return f"ContractedService {self.contracted_service_id} ({self.name})"


# ===========================================================================
# SCREENING DOMAIN
# ===========================================================================
class ScreenStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED = "completed", "Completed"
    DECLINED = "declined", "Declined"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class ScreenType(models.TextChoices):
    STANDARD = "standard", "Standard"
    ENHANCED = "enhanced", "Enhanced"
    ELIGIBILITY = "eligibility", "Eligibility"
    ASSESSMENT = "assessment", "Assessment"
    REASSESSMENT = "reassessment", "Reassessment"
    FOLLOW_UP = "follow_up", "Follow Up"


class OutreachStatus(models.TextChoices):
    NOT_STARTED = "not_started", "Not Started"
    IN_PROGRESS = "in_progress", "In Progress"
    ATTEMPTED = "attempted", "Attempted"
    REACHED = "reached", "Reached"
    UNREACHABLE = "unreachable", "Unreachable"
    COMPLETED = "completed", "Completed"


class Screening(models.Model):
    """An enhanced screening performed for a subject (client)."""

    # --- Core Screening Information ---
    enhanced_screen_id = models.UUIDField(primary_key=True, editable=False)
    subject_id = models.UUIDField(db_index=True)
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="screenings",
    )
    screen_created_at = models.DateTimeField(null=True, blank=True)
    screen_status = models.CharField(
        max_length=50, blank=True  # Free-form: complete, in_progress, pending, etc.
    )
    screen_type = models.CharField(
        max_length=120, blank=True  # Free-form: HM #3, SCN, PHS, etc.
    )
    screen_source = models.CharField(max_length=120, blank=True)

    # --- Provider Info ---
    provider_name = models.CharField(max_length=255, blank=True)
    performing_organization_name = models.CharField(max_length=255, blank=True)

    # --- Screening Content ---
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    questions_answers = models.JSONField(default=list, blank=True)  # [{question, answer}]
    identified_social_needs = models.JSONField(default=list, blank=True)  # [needs from screening]

    # --- Eligibility (if applicable) ---
    eligible_status = models.CharField(max_length=50, blank=True)
    eligible_services = models.JSONField(default=list, blank=True)

    # --- CRM Sync Tracking ---
    crm_opportunity_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    # --- Metadata ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-screen_created_at"]
        indexes = [
            models.Index(fields=["subject_id"]),
            models.Index(fields=["client", "screen_status"]),
            models.Index(fields=["screen_status"]),
            models.Index(fields=["screen_type"]),
        ]

    def __str__(self):
        return f"Screening {self.enhanced_screen_id} ({self.screen_status})"


class Assessment(models.Model):
    """An assessment for a subject (client). Formerly named Eligibility."""

    # --- Core Information ---
    assessment_id = models.UUIDField(primary_key=True, editable=False)
    subject_id = models.UUIDField(db_index=True)  # source client reference
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assessments",
    )
    screen_created_at = models.DateTimeField(null=True, blank=True)
    eligible_status = models.CharField(max_length=50, blank=True)  # e.g., "Eligible", "Not Eligible"
    # The assessment/form type, e.g. "Unite NYC - Food Assistance Assessment".
    form_name = models.CharField(max_length=255, blank=True)

    # --- Provider Info ---
    provider_name = models.CharField(max_length=255, blank=True)  # submitter from Unite Us
    performing_organization_name = models.CharField(max_length=255, blank=True)  # org from Unite Us

    # --- Eligibility Content ---
    # Duration from E-form: "BEFORE STARTING NAVIGATION - What is the duration of the phone call?"
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    questions_answers = models.JSONField(default=list, blank=True)  # [{question, answer}] from Unite Us
    eligible_services = models.JSONField(default=list, blank=True)  # ["Medicaid", "SNAP", etc.]

    # --- CRM Sync Tracking ---
    crm_opportunity_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    # --- Metadata ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-screen_created_at"]
        verbose_name_plural = "assessments"
        indexes = [
            models.Index(fields=["subject_id"]),
            models.Index(fields=["client", "eligible_status"]),
        ]

    def __str__(self):
        return f"Assessment {self.assessment_id} ({self.eligible_status})"


class IdentifiedSocialNeed(models.Model):
    """A social need identified by a screening."""

    identified_social_need_id = models.UUIDField(primary_key=True, editable=False)
    screening = models.ForeignKey(
        Screening, on_delete=models.CASCADE, related_name="social_needs_set"
    )
    identified_social_need_code = models.CharField(max_length=80, blank=True)
    identified_social_need_name = models.CharField(max_length=255, blank=True)
    identified_created_at = models.DateTimeField(null=True, blank=True)
    identified_updated_at = models.DateTimeField(null=True, blank=True)
    is_need_sensitive = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["screening", "identified_social_need_code"])]

    def __str__(self):
        return self.identified_social_need_name or str(self.identified_social_need_id)


class VerifiedSocialNeed(models.Model):
    """A social need verified for a screening."""

    verified_social_need_id = models.UUIDField(primary_key=True, editable=False)
    screening = models.ForeignKey(
        Screening, on_delete=models.CASCADE, related_name="verified_social_needs"
    )
    verified_social_need_code = models.CharField(max_length=80, blank=True)
    verified_social_need_name = models.CharField(max_length=255, blank=True)
    verified_created_at = models.DateTimeField(null=True, blank=True)
    verified_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["screening", "verified_social_need_code"])]

    def __str__(self):
        return self.verified_social_need_name or str(self.verified_social_need_id)


# ===========================================================================
# CLIENT LIFECYCLE / ENROLLMENT
# ===========================================================================
# The acquisition funnel (Client.lifecycle_stage) is auto-derived from synced
# Unite Us data. Service delivery is tracked per-product on an Enrollment, which
# advances through its own stages via guarded manual transitions. StageEvent is
# an append-only audit log of every transition (powers funnel/time-in-stage
# reporting). See api.services.lifecycle for the transition logic.
class EnrollmentStage(models.TextChoices):
    """Service-delivery / verification stage for a household enrollment.

    Verification is a yes/no fact: ``pending_verification`` until the pop-up is
    completed (``verified_at`` set), then ``verified``. The case authorization
    outcome (approved/pending/denied/expired) is a SEPARATE dimension on the
    linked Case -- it gates whether a verified household advances to
    ``kitchen_assignment``, but is NEVER an enrollment stage.
    """

    PENDING_VALIDATION = "pending_validation", "Pending Validation"
    VALIDATED = "validated", "Validated"
    PENDING_VERIFICATION = "pending_verification", "Pending Verification"
    VERIFIED = "verified", "Verified"
    KITCHEN_ASSIGNMENT = "kitchen_assignment", "Kitchen Assignment"
    SERVICE_ACTIVE = "service_active", "Service Active"
    SERVICE_COMPLETE = "service_complete", "Service Complete"
    CLOSED = "closed", "Closed"
    ON_HOLD = "on_hold", "On Hold"
    # Retained for backward-compatibility with existing rows / cancellations;
    # not part of the verification wizard flow.
    CANCELLED = "cancelled", "Cancelled"


class DietaryRestriction(models.TextChoices):
    """Dietary restrictions for a household member (multi-select)."""

    NONE = "none", "None"
    DIABETES = "diabetes", "Diabetes"
    POSTPARTUM = "postpartum", "Postpartum"
    CARDIO_METABOLIC = "cardio_metabolic", "Cardio-metabolic"


class FoodAllergy(models.TextChoices):
    """Food allergies for a household member (multi-select)."""

    NONE = "none", "None"
    SOY = "soy", "Soy"
    WHEAT = "wheat", "Wheat"
    SESAME = "sesame", "Sesame"
    RED_MEAT = "red_meat", "Red Meat"
    PORK = "pork", "Pork"
    MILK = "milk", "Milk"
    EGGS = "eggs", "Eggs"
    FISH = "fish", "Fish"
    SHELLFISH = "shellfish", "Shellfish"
    TREE_NUTS = "tree_nuts", "Tree Nuts"
    PEANUTS = "peanuts", "Peanuts"
    OTHER = "other", "Other"


class MenuCategory(models.TextChoices):
    """Meal category a household member is assigned to (single-select)."""

    FRESH_MEAL = "fresh_meal", "Fresh Meal"
    DAIRY_FREE = "dairy_free", "Dairy Free"
    FISH_FREE = "fish_free", "Fish Free"
    VEGETARIAN = "vegetarian", "Vegetarian"


class MenuType(models.TextChoices):
    """Menu type a household member is assigned to (single-select).

    DEPRECATED for member storage: ``MemberDietaryProfile.menu_type`` now stores
    the admin-managed catalog :class:`MenuType` (model) *name* directly so new
    menu variants (e.g. Kosher, Halal) are usable without code changes. This
    enum is kept for legacy code paths and the data migration that converts old
    codes to names.
    """

    STANDARD = "standard", "Standard"
    FISH_FREE = "fish_free", "Fish Free"
    VEGETARIAN = "vegetarian", "Vegetarian"
    DAIRY_FREE = "dairy_free", "Dairy Free"


class MemberStatus(models.TextChoices):
    """Service status of an individual household member (per enrollment).

    ``OUT_OF_ORBIT`` is set automatically at kitchen-assignment time when the
    member's menu type + food allergies can't be safely fulfilled (see
    ``api.services.meal_rules``). Out-of-orbit members are excluded from all
    delivery schedules and Purchase Orders until their dietary data changes and
    the rule is re-applied.
    """

    ACTIVE = "active", "Active"
    OUT_OF_ORBIT = "out_of_orbit", "Out of Orbit"


# Kitchen meal type that is not a catalog MenuType: signals the kitchen to
# prepare a fully allergen-free meal for a member with an allergy combination.
KITCHEN_MEAL_ALLERGEN_FREE = "Allergen Free"


class ProcessType(models.TextChoices):
    """An operational process run against an enrollment. Extensible: new process
    types (e.g. re-validation) can be added without new tables."""

    VALIDATION = "validation", "Validation"
    VERIFICATION = "verification", "Verification"


class ProcessStatus(models.TextChoices):
    NOT_STARTED = "not_started", "Not Started"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ProcessResult(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"
    NEEDS_FOLLOWUP = "needs_followup", "Needs Follow-up"


class ScheduleStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    RESCHEDULED = "rescheduled", "Rescheduled"


class ScheduleCadence(models.TextChoices):
    ONCE = "once", "One-time"
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Bi-weekly"
    MONTHLY = "monthly", "Monthly"


class StageEntityType(models.TextChoices):
    CLIENT = "client", "Client"
    ENROLLMENT = "enrollment", "Enrollment"


class StageEventSource(models.TextChoices):
    AUTO = "auto", "Auto (derived)"
    MANUAL = "manual", "Manual"


class EnrollmentVerification(models.Model):
    """A household's verification enrollment into the service we deliver. Owns
    the verification/authorization stage, the verification questionnaire data
    (per-member dietary via :class:`MemberDietaryProfile`) and the delivery
    schedule.

    The verification applies to the WHOLE household: ``household`` is the source
    of the participant members. ``client`` remains the primary client (kept for
    funnel/timeline attribution and backward-compatibility).

    The acquisition funnel lives on Client.lifecycle_stage; this picks up once a
    client reaches the ``client`` stage and we begin validation/verification/
    service.
    """

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="enrollments"
    )
    # The household this verification applies to; participants come from its
    # members. Optional until a household exists for the client.
    household = models.ForeignKey(
        "Household", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollment_verifications",
    )
    # The Met Council case this enrollment is delivered under (optional until a
    # case exists).
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollments",
    )
    # The kitchen assigned to fulfill this household's deliveries. One kitchen
    # serves the whole household (members are never split across kitchens). Set
    # on the Logistics page; editable from the member profile. NULL until
    # assigned.
    kitchen = models.ForeignKey(
        "Kitchen", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollment_verifications",
    )
    stage = models.CharField(
        max_length=25, choices=EnrollmentStage.choices,
        default=EnrollmentStage.PENDING_VERIFICATION, db_index=True,
    )
    # The program the household is participating in (Step 1; free text taken
    # from the case/contracted service, e.g. "Medically Tailored Meals").
    program_name = models.CharField(max_length=255, blank=True)
    # The case's Service Type (list-view value, e.g. "Food Insecurity").
    # Snapshotted from the linked Case.service_type when the enrollment is
    # created from the extension.
    service_type = models.CharField(max_length=255, blank=True)
    # Call-transfer outcome captured on the E-Form when the verification was
    # requested (snapshot of the same value also stored on the Client).
    call_transfer_answered = models.CharField(
        max_length=30, choices=CallTransferStatus.choices, blank=True
    )
    # Household size snapshot captured during the wizard (Step 1).
    household_size = models.PositiveSmallIntegerField(null=True, blank=True)
    # Weekday codes the customer wants deliveries on (agent-entered), e.g.
    # ["mon", "thu"]. The count == deliveries per week; expanded across the
    # case authorization window when orders are generated. Codes are the
    # lowercase 3-letter day names (mon/tue/wed/thu/fri/sat/sun).
    delivery_weekdays = models.JSONField(default=list, blank=True)
    # Delivery address shared by all participants (Step 2 - Delivery Address).
    delivery_address = models.ForeignKey(
        "Address", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollment_verifications",
    )
    # --- Step 4: Validation checks (tri-state: null = not checked yet). ---
    is_family_verified = models.BooleanField(null=True, blank=True)
    medicaid_type_verified = models.BooleanField(null=True, blank=True)
    delivery_address_verified = models.BooleanField(null=True, blank=True)
    # Verification fact: set when the verification pop-up is COMPLETED (the job
    # that captures food allergies, delivery address, the Step-4 checks). This --
    # not the stage -- is the source of truth for "is this household verified?".
    # NULL = pending_verification; set = verified. Never touched by the data
    # import; only the pop-up (or a one-off backfill) sets it.
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    verified_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verified_enrollments",
    )
    # Short display code, e.g. "ENR-8754". Assigned on creation.
    code = models.CharField(max_length=20, blank=True, db_index=True)
    # Renewal cycle counter. Renewals reuse the SAME enrollment (re-run
    # screening/assessment/verification) rather than creating a new row; this
    # tracks which cycle the enrollment is on (1 = initial, 2 = first renewal…).
    renewal_number = models.PositiveSmallIntegerField(default=1)
    stage_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["client", "stage"]),
            models.Index(fields=["household", "stage"]),
        ]
        constraints = [
            # At most one verification per (navigation) case. Renewals reuse the
            # same row, so this never blocks a renewal. NULL case is unconstrained.
            models.UniqueConstraint(
                fields=["case"],
                condition=models.Q(case__isnull=False),
                name="uniq_enrollment_verification_per_case",
            ),
        ]

    def __str__(self):
        return f"{self.client_id} ({self.stage})"


class MemberDietaryProfile(models.Model):
    """Per-household-member dietary profile captured during the household
    verification (wizard Step 2).

    The household is the unit of verification (the outcome lives on
    ``EnrollmentVerification.stage``); this row only holds a participant's
    dietary data, which is used to assign a product type + menu and build the
    delivery plan. One row per participant in an enrollment's household.
    Dietary restrictions and food allergies are multi-select (stored as lists
    of choice codes); meal category and menu type are single-select.
    """

    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE,
        related_name="member_profiles",
    )
    # The participant client this row is for. Snapshot ``member_name`` is kept
    # so the row stays readable even if the client link is later cleared.
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="member_profiles",
    )
    member_name = models.CharField(max_length=255, blank=True)
    # Multi-select choice-code lists (validated in the serializer against
    # DietaryRestriction / FoodAllergy). Default empty == nothing selected.
    dietary_restrictions = models.JSONField(default=list, blank=True)
    food_allergies = models.JSONField(default=list, blank=True)
    other_dietary_restrictions = models.TextField(blank=True)
    meal_category = models.CharField(
        max_length=20, choices=MenuCategory.choices, blank=True
    )
    # Stores the admin-managed catalog ``MenuType`` (model) NAME, e.g.
    # "Standard", "Dairy Free", "Vegetarian", "Kosher", "Halal". (Historically
    # this held a short code from the ``MenuType`` TextChoices; a data migration
    # converts those to names.)
    menu_type = models.CharField(max_length=120, blank=True)
    # Per-member service status. Set to OUT_OF_ORBIT automatically when the meal
    # rule (api.services.meal_rules) can't safely fulfill the member.
    status = models.CharField(
        max_length=20, choices=MemberStatus.choices,
        default=MemberStatus.ACTIVE, db_index=True,
    )
    # Result of applying the meal rule at kitchen-assignment time. These (not
    # ``menu_type``/derived food notes) are what we send to the kitchen on the
    # Purchase Order for each member.
    kitchen_meal_type = models.CharField(max_length=120, blank=True)
    kitchen_food_notes = models.TextField(blank=True)
    # Agent-entered: how many meals/boxes this member receives per delivery.
    # Copied onto each generated OrderSchedule.how_many_meals_or_boxes.
    meals_per_delivery = models.PositiveSmallIntegerField(null=True, blank=True)
    general_verification_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "client"],
                name="uniq_member_dietary_profile_per_enrollment_client",
            ),
        ]
        indexes = [
            models.Index(fields=["enrollment"]),
        ]

    def __str__(self):
        return f"Dietary profile for {self.member_name or self.client_id} (enrollment {self.enrollment_id})"


class EnrollmentProcess(models.Model):
    """A validation/verification (or future) process run for an enrollment.

    Process-specific captured data (validated delivery address, household size,
    verification answers, etc.) is stored in ``data`` as flexible JSON; canonical
    values still live on Client/Address. When the verification questionnaire
    firms up, ``data`` can graduate into structured question/answer tables.
    """

    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE, related_name="processes"
    )
    process_type = models.CharField(
        max_length=20, choices=ProcessType.choices, db_index=True
    )
    status = models.CharField(
        max_length=20, choices=ProcessStatus.choices,
        default=ProcessStatus.NOT_STARTED,
    )
    result = models.CharField(
        max_length=20, choices=ProcessResult.choices, blank=True
    )
    data = models.JSONField(default=dict, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollment_processes",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["enrollment", "process_type", "status"]),
        ]

    def __str__(self):
        return f"{self.get_process_type_display()} for enrollment {self.enrollment_id} ({self.status})"


class ServiceSchedule(models.Model):
    """When services are delivered for an enrollment. Supports one-time and
    recurring cadences. Individual delivery occurrences/visits can be modeled
    separately later if needed.
    """

    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE, related_name="schedules"
    )
    service = models.ForeignKey(
        Service, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="schedules",
    )
    cadence = models.CharField(
        max_length=20, choices=ScheduleCadence.choices, default=ScheduleCadence.ONCE
    )
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=ScheduleStatus.choices,
        default=ScheduleStatus.SCHEDULED,
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduled_start"]
        indexes = [
            models.Index(fields=["enrollment", "status"]),
            models.Index(fields=["scheduled_start"]),
        ]

    def __str__(self):
        return f"Schedule for enrollment {self.enrollment_id} ({self.status})"


class MemberDeliverySchedule(models.Model):
    """A single household member's recurring delivery PLAN for an enrollment.

    Created once a verification is completed and its case authorization is
    Accepted. It is the durable source of truth for "what should be delivered to
    this member each week" and is expanded into dated :class:`OrderSchedule`
    occurrences (the delivery calendar) across the case authorization window.

    Cadence and per-delivery quantity come from ``product_type`` but are
    snapshotted here so the plan stays stable even if the ProductType row later
    changes. ``menu_type`` is snapshotted from the member's verification answers.
    """

    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE,
        related_name="delivery_schedules",
    )
    # The member this plan is for. ``member_profile`` links back to the dietary
    # profile; ``household_member`` is the durable household membership.
    household_member = models.ForeignKey(
        HouseholdMember, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_schedules",
    )
    member_profile = models.ForeignKey(
        MemberDietaryProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_schedules",
    )
    member_name = models.CharField(max_length=255, blank=True)
    program = models.ForeignKey(
        Program, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_schedules",
    )
    # Source of cadence + per-delivery quantity (snapshotted below).
    product_type = models.ForeignKey(
        ProductType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_schedules",
    )
    # Snapshot of the household's assigned kitchen at the time the plan was
    # created (the household-level assignment lives on the enrollment).
    kitchen = models.ForeignKey(
        "Kitchen", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_schedules",
    )
    # Snapshots taken from ``product_type`` at creation.
    delivery_days_cadence = models.CharField(
        max_length=20, choices=DeliveryCadence.choices, blank=True
    )
    prod_per_delivery = models.PositiveSmallIntegerField(default=0)
    # Snapshot of ProductType.meals_per_day for meals plans (0 for boxes). The
    # per-delivery quantity is meals_per_day * the days each delivery covers.
    meals_per_day = models.PositiveSmallIntegerField(default=0)
    # Total meals/boxes across the authorization window. For boxes this is
    # prod_per_delivery * number of delivery dates; for meals it is the sum of
    # each delivery's coverage * meals_per_day.
    meals_boxes_total = models.PositiveIntegerField(default=0)
    # Snapshot of the member's catalog MenuType NAME (see MemberDietaryProfile).
    menu_type = models.CharField(max_length=120, blank=True)
    # Snapshot of the per-member meal-rule result (api.services.meal_rules).
    kitchen_meal_type = models.CharField(max_length=120, blank=True)
    kitchen_food_notes = models.TextField(blank=True)
    # Snapshot of the case authorization window the plan covers.
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=ScheduleStatus.choices,
        default=ScheduleStatus.SCHEDULED, db_index=True,
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["enrollment", "status"]),
            models.Index(fields=["household_member"]),
        ]
        constraints = [
            # One plan per member per program within an enrollment.
            models.UniqueConstraint(
                fields=["enrollment", "household_member", "program"],
                name="uniq_member_delivery_schedule",
            ),
        ]

    def __str__(self):
        who = self.member_name or self.household_member_id
        return f"Delivery plan for {who} ({self.delivery_days_cadence or 'no cadence'})"


# ===========================================================================
# ORDER / DELIVERY DOMAIN
# ===========================================================================
class OrderStatus(models.TextChoices):
    """Fulfillment status of a single member's delivery order."""

    SCHEDULED = "scheduled", "Scheduled"
    ON_THE_KITCHEN = "on_the_kitchen", "On the Kitchen"
    ON_THE_WAY = "on_the_way", "On the Way"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"


def generate_household_group_code():
    """A short, human-readable identifier (e.g. ``HG-3F9A2C``) shared by every
    order in the same household delivery batch, so the kitchen and delivery
    company can group and deliver a household's food together. Generated once
    per batch and assigned to each :class:`OrderSchedule` in that batch."""
    return f"HG-{uuid.uuid4().hex[:6].upper()}"


class OrderSchedule(models.Model):
    """A single member's food delivery order, generated when a verification
    enrollment is saved. One row per participant per delivery; orders for the
    same household batch share a ``household_group_code`` so they ship together.

    The member-facing fields (name/phone/email/allergies/restrictions/menu_type/
    delivery_address) are SNAPSHOTS taken at creation, so a historical order
    stays accurate even if the member or enrollment later changes.
    """

    order_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE, related_name="orders"
    )
    program_name = models.CharField(max_length=255, blank=True)
    # The participant this order is for (the wizard's per-member row).
    member = models.ForeignKey(
        MemberDietaryProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders",
    )
    member_name = models.CharField(max_length=255, blank=True)
    anticipated_delivery_date = models.DateField(null=True, blank=True)
    # External system identifiers (no local Kitchen/DeliveryCompany tables yet);
    # empty until the order is dispatched.
    sent_to_kitchen_id = models.CharField(max_length=120, blank=True)
    sent_to_delivery_company_id = models.CharField(max_length=120, blank=True)
    # The household these deliveries belong to + the shared batch code. Every
    # order delivered together for a household carries the same group code.
    household = models.ForeignKey(
        "Household", on_delete=models.PROTECT, null=True, blank=True,
        related_name="orders",
    )
    household_group_code = models.CharField(max_length=20, db_index=True)
    # Snapshot of the household's DEFAULT (assigned) kitchen at calendar
    # generation. Used to aggregate the delivery calendar into purchase orders
    # by kitchen. The ACTUAL kitchen a delivery is fulfilled by lives on the
    # DeliveryOrder (it may be rerouted at PO time); this stays the default.
    kitchen = models.ForeignKey(
        "Kitchen", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="order_schedules",
    )
    status = models.CharField(
        max_length=20, choices=OrderStatus.choices,
        default=OrderStatus.SCHEDULED, db_index=True,
    )
    delivery_address = models.TextField(blank=True)
    address_notes = models.TextField(blank=True)
    # Multi-select snapshots (choice-code lists, validated in the serializer
    # against FoodAllergy / DietaryRestriction).
    allergies = models.JSONField(default=list, blank=True)
    restrictions = models.JSONField(default=list, blank=True)
    # Snapshot of the member's catalog MenuType NAME (see MemberDietaryProfile).
    menu_type = models.CharField(max_length=120, blank=True)
    # Snapshot of the per-member meal-rule result (api.services.meal_rules).
    kitchen_meal_type = models.CharField(max_length=120, blank=True)
    kitchen_food_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    member_phone = models.CharField(max_length=40, blank=True)
    member_email = models.EmailField(blank=True)
    how_many_meals_or_boxes = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["enrollment", "status"]),
            models.Index(fields=["household_group_code"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Order {self.order_id} for {self.member_name or self.member_id} ({self.status})"


class OrderDeliveryProof(models.Model):
    """A proof-of-delivery image for an order. The binary lives in S3; this row
    stores the object key + public/signed URL and upload metadata. An order can
    have many proofs."""

    order = models.ForeignKey(
        OrderSchedule, on_delete=models.CASCADE, related_name="proofs"
    )
    s3_key = models.CharField(max_length=500)
    file_url = models.URLField(blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="order_delivery_proofs",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]
        indexes = [
            models.Index(fields=["order"]),
        ]

    def __str__(self):
        return f"Proof for order {self.order_id} ({self.s3_key})"


class StageEvent(models.Model):
    """Append-only audit log of a stage transition on either a Client (funnel)
    or an Enrollment (service delivery). Mirrors the nullable-FK pattern used by
    Answer (screening/eligibility). Powers funnel conversion and time-in-stage
    reporting.
    """

    entity_type = models.CharField(
        max_length=20, choices=StageEntityType.choices, db_index=True
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, null=True, blank=True,
        related_name="stage_events",
    )
    enrollment = models.ForeignKey(
        EnrollmentVerification, on_delete=models.CASCADE, null=True, blank=True,
        related_name="stage_events",
    )
    from_stage = models.CharField(max_length=25, blank=True)
    to_stage = models.CharField(max_length=25)
    source = models.CharField(
        max_length=10, choices=StageEventSource.choices,
        default=StageEventSource.AUTO,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stage_events",
    )
    note = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-entered_at"]
        indexes = [
            models.Index(fields=["entity_type", "to_stage"]),
            models.Index(fields=["client", "entered_at"]),
            models.Index(fields=["enrollment", "entered_at"]),
        ]

    def __str__(self):
        ref = self.enrollment_id or self.client_id
        return f"{self.entity_type} {ref}: {self.from_stage or '-'} -> {self.to_stage}"


class Agent(models.Model):
    """Agent/Employee who uses the extension system. Validated by agent code."""

    AGENT_GROUPS = [
        ("Screeners", "Screeners"),
        ("Verifiers", "Verifiers"),
        ("Logistics", "Logistics"),
        ("Management", "Management"),
        ("CS", "CS"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    # agent_code is the value GHL + the extension use; sourced from the CallTools
    # dialer extension when synced. Nullable: users without an extension are still
    # stored (for identity/features) but can't authenticate by code. NULLs are
    # treated as distinct, so multiple code-less agents are allowed.
    agent_code = models.CharField(
        max_length=20, unique=True, null=True, blank=True, db_index=True
    )
    group = models.CharField(max_length=50, choices=AGENT_GROUPS, default="Screeners")
    status = models.CharField(max_length=20, default="Active")
    cbo = models.CharField(max_length=255, blank=True, default="Met Council")

    # --- CallTools dialer identity (synced from /api/users/) ---
    calltools_app_user = models.UUIDField(null=True, blank=True, unique=True, db_index=True)
    email = models.EmailField(blank=True)
    username = models.CharField(max_length=150, blank=True)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    # Job title and department (sourced from the company directory CSV).
    title = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    is_agent = models.BooleanField(default=True)
    is_manager = models.BooleanField(default=False)
    is_account_owner = models.BooleanField(default=False)
    calltools_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["agent_code", "status"]),
            models.Index(fields=["group", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.agent_code}) - {self.group}"


class UniteUsAgent(models.Model):
    """A Unite Us (Unite NYC / SCN) platform user, sourced from the Unite Us
    users export.

    These are SEPARATE from :class:`Agent` (our extension/CallTools users) -- they
    are Met Council / network staff who create cases in Unite Us and have nothing
    to do with our agent accounts. Their ``user_id`` matches ``Case.created_by_id``
    (a perfect, complete join in the data), so this managed list is used purely to
    filter imported cases by the Unite Us user who created them.

    Managed from Settings (add/remove) and seedable from the users export.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The Unite Us user_id == Case.created_by_id. Unique natural key; required so
    # the entry can actually match (and filter) cases.
    user_id = models.UUIDField(unique=True, db_index=True)
    # Optional Unite Us employee_id (a different id; kept for reference only).
    employee_id = models.UUIDField(null=True, blank=True)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    work_title = models.CharField(max_length=255, blank=True)
    # Employee status from the export ("active"/"inactive"); informational.
    status = models.CharField(max_length=20, blank=True, default="active")
    # Whether this person is a Unite Us ("US") / CareCircle agent, sourced from
    # the CareCircle team roster ("Us?" column). Anyone NOT on the roster is
    # treated as Met Council staff (is_us=False).
    is_us = models.BooleanField(default=False)
    # The team this agent originates from, from the CareCircle roster
    # ("Originating Team", e.g. "CareCircle Call Center"). Defaults to
    # "Met Council Team" for everyone not on the roster.
    originating_team = models.CharField(max_length=120, blank=True, default="Met Council Team")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Unite Us agent"
        verbose_name_plural = "Unite Us agents"

    def __str__(self):
        return f"{self.name or self.email or self.user_id}"


class AgentLoginCode(models.Model):
    """A short-lived, single-use 2FA code emailed to an agent's company email to
    complete extension login.

    The plaintext code is never stored — only a salted hash (Django's password
    hashers). A code expires after a short TTL, is single-use (``consumed_at``),
    and caps the number of verification attempts to resist brute force.
    """

    CODE_LENGTH = 6

    class Source(models.TextChoices):
        # The app a code was requested from. Codes are scoped per source so a
        # login attempt in one app doesn't invalidate a pending code in the
        # other (an agent may use both the extension and the support portal).
        EXTENSION = "extension", "Extension"
        PORTAL = "portal", "Support portal"

    email = models.EmailField(db_index=True)
    # The active agent this code was issued for (resolved from the email at
    # request time). Kept for auditing and to mint the JWT on verify.
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        related_name="login_codes",
        null=True,
        blank=True,
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.EXTENSION,
        db_index=True,
    )
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "expires_at"])]

    def __str__(self):
        return f"2FA code for {self.email} (exp {self.expires_at:%Y-%m-%d %H:%M})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self):
        return self.consumed_at is not None

    @staticmethod
    def generate_code():
        """A zero-padded numeric code of length ``CODE_LENGTH`` (e.g. "042913")."""
        upper = 10 ** AgentLoginCode.CODE_LENGTH
        return f"{secrets.randbelow(upper):0{AgentLoginCode.CODE_LENGTH}d}"

    @classmethod
    def issue(cls, email, agent=None, ttl_seconds=None, source=None):
        """Create and store a new code for ``email``; returns ``(instance, code)``.

        Only the hash is persisted; the returned plaintext ``code`` is for
        delivery (email) and is not recoverable afterwards. ``source`` scopes the
        code to the requesting app (extension vs portal) so codes don't collide.
        """
        ttl = ttl_seconds or getattr(settings, "AGENT_2FA_CODE_TTL_SECONDS", 600)
        code = cls.generate_code()
        obj = cls.objects.create(
            email=(email or "").strip().lower(),
            agent=agent,
            source=source or cls.Source.EXTENSION,
            code_hash=make_password(code),
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return obj, code

    def check_code(self, code):
        """True when ``code`` matches the stored hash."""
        return check_password(str(code or "").strip(), self.code_hash)


class HouseholdMemberLoginCode(models.Model):
    """A short-lived, single-use 2FA code for the Benefully member mobile app.

    Mirrors :class:`AgentLoginCode` but is keyed by a mobile number (the
    member's app username) and links to the :class:`HouseholdMember` when one is
    matched. Only a salted hash of the code is stored; codes expire, are
    single-use, and cap verification attempts.

    Delivery is intended to be SMS (Twilio). Until that's wired up the code is
    emailed to an operator inbox instead (see ``views_member_app``).
    """

    CODE_LENGTH = 6

    member = models.ForeignKey(
        "HouseholdMember",
        on_delete=models.CASCADE,
        related_name="login_codes",
        null=True,
        blank=True,
    )
    mobile_number = models.CharField(max_length=32, db_index=True)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["mobile_number", "expires_at"])]

    def __str__(self):
        return f"Member 2FA code for {self.mobile_number} (exp {self.expires_at:%Y-%m-%d %H:%M})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self):
        return self.consumed_at is not None

    @staticmethod
    def generate_code():
        """A zero-padded numeric code of length ``CODE_LENGTH`` (e.g. "042913")."""
        upper = 10 ** HouseholdMemberLoginCode.CODE_LENGTH
        return f"{secrets.randbelow(upper):0{HouseholdMemberLoginCode.CODE_LENGTH}d}"

    @classmethod
    def issue(cls, mobile_number, member=None, ttl_seconds=None):
        """Create and store a new code for ``mobile_number``; returns ``(instance, code)``.

        Only the hash is persisted; the returned plaintext ``code`` is for
        delivery (SMS/email) and is not recoverable afterwards.
        """
        ttl = ttl_seconds or getattr(settings, "AGENT_2FA_CODE_TTL_SECONDS", 600)
        code = cls.generate_code()
        obj = cls.objects.create(
            mobile_number=(mobile_number or "").strip(),
            member=member,
            code_hash=make_password(code),
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return obj, code

    def check_code(self, code):
        """True when ``code`` matches the stored hash."""
        return check_password(str(code or "").strip(), self.code_hash)


class ProgramPipeline(models.Model):
    """Maps a source Program Name to the GHL pipeline a case should sync to.

    Cases are routed to a GHL opportunity pipeline by looking up the case's
    ``program_name`` here. The authoritative routing value is ``pipeline_id``
    (the ``pipeline_name`` column in the source data has typos and is for human
    reference only). The category columns support a fallback when a case's
    program name is not found in this table.
    """

    program_name = models.CharField(max_length=255, unique=True, db_index=True)
    main_category = models.CharField(max_length=120, blank=True)
    # ELIGIBILITY / NAVIGATION / Internal Services / External Services, etc.
    case_category = models.CharField(max_length=120, blank=True, db_index=True)
    services_category = models.CharField(max_length=120, blank=True)
    pipeline_name = models.CharField(max_length=120, blank=True)  # human label
    pipeline_id = models.CharField(max_length=64)  # authoritative GHL pipeline id
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["program_name"]
        indexes = [
            models.Index(fields=["case_category"]),
        ]

    def __str__(self):
        return f"{self.program_name} -> {self.pipeline_name} ({self.pipeline_id})"


class AllowedZipCode(models.Model):
    """A ZIP code we are allowed to serve. Agents may only request verification
    for clients whose primary address ZIP is in this table. Imported from
    ``tmp/ZipCodes.csv`` via ``manage.py import_zipcodes``."""

    zip_code = models.CharField(max_length=10, unique=True, db_index=True)
    borough = models.CharField(max_length=255, blank=True)  # Borough/Neighborhood
    scn = models.CharField(max_length=120, blank=True)
    platform = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["zip_code"]

    def __str__(self):
        return f"{self.zip_code} ({self.borough})" if self.borough else self.zip_code


# ===========================================================================
# UNITE US INTEGRATION / DAILY PULL
# ===========================================================================
class UniteUsCredentialStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXPIRED = "expired", "Expired"
    REVOKED = "revoked", "Revoked"


class UniteUsCredential(models.Model):
    """Per-provider Unite Us OAuth credentials captured by the extension on agent
    login and refreshed server-side for the daily pull. The access/refresh token
    columns are encrypted at rest (see api.fields.EncryptedTextField)."""

    provider_id = models.CharField(max_length=64, db_index=True)  # x-provider-id
    employee_id = models.CharField(max_length=64, blank=True, db_index=True)  # x-employee-id
    agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="uniteus_credentials",
    )
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=255, blank=True)
    token_type = models.CharField(max_length=40, blank=True, default="Bearer")
    status = models.CharField(
        max_length=20, choices=UniteUsCredentialStatus.choices,
        default=UniteUsCredentialStatus.ACTIVE, db_index=True,
    )
    last_captured_at = models.DateTimeField(null=True, blank=True)  # last extension push
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_id", "employee_id"],
                name="unique_uniteus_provider_employee",
            )
        ]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"UniteUs cred provider={self.provider_id} ({self.status})"


class ImportRunStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ImportRun(models.Model):
    """One execution of the daily Unite Us pull. Run-level audit anchor: holds
    per-dataset counts, errors, and the tickets it raised. Per-entity field
    diffs live in the django-simple-history tables, tagged source='import'."""

    source = models.CharField(max_length=40, default="uniteus")
    status = models.CharField(
        max_length=20, choices=ImportRunStatus.choices,
        default=ImportRunStatus.PENDING, db_index=True,
    )
    triggered_by = models.CharField(max_length=120, blank=True)  # cron | agent:355 | manual
    # For manual CSV uploads processed asynchronously (S3 + Celery): the dataset
    # being imported, the S3 object key of the uploaded file (kept for history /
    # re-run), and the original filename the agent uploaded. Blank for the daily
    # API pull and legacy synchronous uploads.
    export_type = models.CharField(max_length=40, blank=True)
    file_key = models.CharField(max_length=512, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Per-dataset breakdown, e.g.
    # {"cases": {"created": 1, "updated": 2, "skipped": 0, "errors": 0}, ...}
    # For case imports, stats["actions"] also holds an aggregate of the
    # follow-up actions detected (cases closed, auth changes, tickets), with
    # "applied": false in preview mode (detected but not yet created).
    stats = models.JSONField(default=dict, blank=True)
    # Preview of the individual follow-up tickets a case import WOULD open (or
    # did open, when applied), so an agent can review before/after the run.
    # A capped list of {case_id, client_id, action, reason}. Empty otherwise.
    planned_actions = models.JSONField(default=list, blank=True)
    # Total rows to process (pre-counted) so the UI can show a true percentage;
    # null while unknown. ``processed_count`` below is the running numerator.
    progress_total = models.PositiveIntegerField(null=True, blank=True)
    processed_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_log = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "status"])]

    def __str__(self):
        return f"ImportRun {self.pk} {self.source} ({self.status})"


# ===========================================================================
# NOTES (append-only)
# ===========================================================================
class NoteSource(models.TextChoices):
    UNITE_US = "unite_us", "Unite Us"
    AGENT = "agent", "Agent"
    SYSTEM = "system", "System"


class Note(models.Model):
    """Append-only note attached to a Client and/or Case. Imported from Unite Us
    or authored by an agent; never overwritten. De-duped on source_note_id (or
    content_hash + source_created_at) so re-runs don't duplicate."""

    client = models.ForeignKey(
        "Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notes",
    )
    case = models.ForeignKey(
        "Case", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notes",
    )
    source = models.CharField(
        max_length=20, choices=NoteSource.choices, default=NoteSource.UNITE_US
    )
    source_note_id = models.CharField(max_length=64, blank=True, db_index=True)
    author_name = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    history = tracked_history()

    class Meta:
        ordering = ["-source_created_at", "-created_at"]
        indexes = [
            models.Index(fields=["client", "source_created_at"]),
            models.Index(fields=["case", "source_created_at"]),
            models.Index(fields=["source", "source_note_id"]),
        ]

    def __str__(self):
        ref = self.case_id or self.client_id
        return f"Note {self.pk} ({self.source}) for {ref}"


# ===========================================================================
# AGENT FOLLOW-UP TICKETS
# ===========================================================================
class TicketTypeCode(models.TextChoices):
    """Canonical ticket-type codes. The :class:`TicketType` table is seeded from
    these; the codes stay stable so services can keep referencing them
    symbolically (e.g. ``TicketTypeCode.SYSTEM_CHANGE_DETECTED``).

    The first block is the human-facing set offered in the New-Ticket picker.
    ``SYSTEM_CHANGE_DETECTED`` is raised by the system (daily import / update
    feature) when it detects a change on a member's information; it is hidden
    from the manual picker. Legacy auto-pull codes (no_active_insurance,
    case_closed, …) remain in the database as inactive types for historical
    tickets but are no longer raised — the import now opens
    ``SYSTEM_CHANGE_DETECTED`` with a descriptive reason instead."""

    VERIFICATION = "verification", "Verification"
    APPOINTMENT = "appointment", "Appointment"
    SERVICE_CHANGE = "service_change", "Service Change"
    DELIVERY_ISSUE = "delivery_issue", "Delivery Issue"
    ADDRESS_UPDATE = "address_update", "Address Update"
    CASE_CLOSURE = "case_closure", "Case Closure"
    FOOD_COMPLAINT = "food_complaint", "Food Complaint"
    PAUSE_SERVICE = "pause_service", "Pause Service"
    STATUS_CHECK = "status_check", "Status Check"
    LOGIN_PROBLEM = "login_problem", "Login Problem"
    CANCELLATION = "cancellation", "Cancellation"
    MISSING_WRONG_ORDER = "missing_wrong_order", "Missing / Wrong Order"
    # Raised by the daily import when an internal-service case has no contracted
    # services (the member has no active internal-services contract).
    CASE_NO_SERVICES = "case_no_services", "No Internal services Case"
    SYSTEM_CHANGE_DETECTED = "system_change_detected", "System Change Detected"


class TicketSource(models.TextChoices):
    """Where a ticket originated, captured when an agent opens one manually."""

    LIVE_CALL = "live_call", "Live Call"
    CALL_BACK = "call_back", "Call Back"
    ASSIGNED_TICKET = "assigned_ticket", "Assigned Ticket"
    DELIVERY_ISSUE = "delivery_issue", "Delivery Issue"
    EMAIL = "email", "Email"
    OTHER = "other", "Other"


class TicketStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In Progress"
    RESOLVED = "resolved", "Resolved"


class TicketSeverity(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class TicketType(models.Model):
    """A type/category of agent follow-up ticket. Seeded from
    :class:`TicketTypeCode` but stored in the database so new types can be added
    (and labels/descriptions/default severity tuned) from the admin without a
    code change."""

    ticket_type_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    code = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    default_severity = models.CharField(
        max_length=10, choices=TicketSeverity.choices, default=TicketSeverity.MEDIUM
    )
    # Whether this type is offered when manually creating a ticket. Inactive
    # types stay valid for historical/auto-raised tickets but are hidden from
    # the create picker.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return self.label


class Ticket(models.Model):
    """An agent follow-up item raised by the daily pull when it detects a
    situation a human must review. References the related Client/Case and the
    ImportRun that raised it."""

    type = models.ForeignKey(
        TicketType, on_delete=models.PROTECT, related_name="tickets"
    )
    status = models.CharField(
        max_length=20, choices=TicketStatus.choices,
        default=TicketStatus.OPEN, db_index=True,
    )
    severity = models.CharField(
        max_length=10, choices=TicketSeverity.choices, default=TicketSeverity.MEDIUM
    )
    # Where the ticket came from (set when an agent opens one manually; blank for
    # system-raised tickets such as SYSTEM_CHANGE_DETECTED).
    source = models.CharField(
        max_length=20, choices=TicketSource.choices, blank=True, default=""
    )
    reason = models.TextField(blank=True)  # human-readable explanation
    client = models.ForeignKey(
        "Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )
    case = models.ForeignKey(
        "Case", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )
    import_run = models.ForeignKey(
        "ImportRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )
    assigned_to = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=120, blank=True)  # agent:355 / user:alex
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = tracked_history()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "type"]),
            models.Index(fields=["client", "status"]),
            models.Index(fields=["case", "status"]),
        ]

    def __str__(self):
        return f"Ticket {self.pk} {self.type} ({self.status})"


# ===========================================================================
# CLIENT TIMELINE (central history)
# ===========================================================================
class TimelineEventType(models.TextChoices):
    CONSENT_GRANTED = "consent_granted", "Consent Granted"
    INSURANCE = "insurance", "Insurance"
    SOCIAL_CARE_COVERAGE = "social_care_coverage", "Social Care Coverage"
    SCREENING = "screening", "Screening"
    ASSESSMENT = "assessment", "Assessment"
    CASE_OPENED = "case_opened", "Case"
    CASE_STATUS_CHANGED = "case_status_changed", "Case Status Changed"
    CASE_AUTH_CHANGED = "case_auth_changed", "Case Authorization Changed"
    # --- Verification / authorization stages: one granular type per stage so
    # the History tab reads each transition distinctly instead of a pile of
    # generic "Verification" rows. ---
    PENDING_VALIDATION = "pending_validation", "Pending Validation"
    VALIDATED = "validated", "Validated"
    VERIFICATION_REQUESTED = "verification_requested", "Verification Requested"
    VERIFICATION_COMPLETED = "verification_completed", "Verification Completed"
    WAITING_AUTHORIZATION = "waiting_authorization", "Waiting Authorization"
    AUTHORIZED = "authorized", "Authorized"
    DENIED = "denied", "Denied"
    # --- Service-delivery lifecycle: one granular type per event. ---
    KITCHEN_ASSIGNED = "kitchen_assigned", "Kitchen Assigned"
    SERVICE_ACTIVATED = "service_activated", "Service Activated"
    SERVICE_ON_HOLD = "service_on_hold", "Service On Hold"
    SERVICE_RESUMED = "service_resumed", "Service Resumed"
    SERVICE_COMPLETED = "service_completed", "Service Completed"
    SERVICE_CLOSED = "service_closed", "Service Closed"
    SERVICE_CANCELLED = "service_cancelled", "Service Cancelled"
    ENROLLED = "enrolled", "Enrolled"
    # --- Other client-lifecycle events not tied to a stage transition. ---
    TICKET_CREATED = "ticket_created", "New Ticket Created"
    DELIVERY_ADDRESS_CHANGED = "delivery_address_changed", "Delivery Address Changed"
    OUT_OF_ORBIT = "out_of_orbit", "Out of Orbit"
    MEMBER_REACTIVATED = "member_reactivated", "Member Reactivated"
    HOUSEHOLD_MEMBER_ADDED = "household_member_added", "Household Member Added"
    # --- Legacy coarse types: retained so existing rows stay valid; no longer
    # emitted by the timeline service (a data migration remaps old rows). ---
    VERIFICATION = "verification", "Verification"
    SERVICE = "service", "Service"


class TimelineBadgeTone(models.TextChoices):
    """Visual tone for the right-hand badge on a timeline row."""

    NEUTRAL = "neutral", "Neutral"
    INFO = "info", "Info"
    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"
    DANGER = "danger", "Danger"


class TimelineEvent(models.Model):
    """A single entry in a client's central history/timeline.

    A denormalized, append-style event stream that unifies heterogeneous domain
    events (consent, insurance, coverage, screening, assessment, case,
    verification, enrollment) into one ordered list. Each event carries display
    fields (title/subtitle/badge) plus a generic link to its source entity so
    the UI can deep-link to the underlying record. Written at each capture point
    via ``api.services.timeline.emit_timeline_event`` and de-duped on
    ``dedupe_key`` so re-imports/re-saves don't create duplicates.
    """

    client = models.ForeignKey(
        "Client", on_delete=models.CASCADE, related_name="timeline_events"
    )
    # The enrollment whose renewal cycle this event belongs to (set once an
    # enrollment exists; null for early-funnel events like the first consent /
    # screening). Renewal grouping in the UI keys off (enrollment, renewal_number).
    enrollment = models.ForeignKey(
        "EnrollmentVerification", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="timeline_events",
    )
    # The Case this event pertains to (set for case-scoped events: case opened /
    # status change / auth change, verification stages via the enrollment's case,
    # and tickets tied to a case). Enables a case-scoped history view and lets the
    # client timeline group rows by case. Null for client-level events (consent,
    # insurance, coverage).
    case = models.ForeignKey(
        "Case", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="timeline_events",
    )
    # Renewal cycle the event occurred in: 1 = initial, 2 = first renewal, …
    # The UI renders a "Renewal #N" group header for N >= 2.
    renewal_number = models.PositiveSmallIntegerField(default=1, db_index=True)
    event_type = models.CharField(
        max_length=30, choices=TimelineEventType.choices, db_index=True
    )
    # The domain date shown on the row (e.g. screen_created_at, enrolled_at),
    # NOT the row's insert time (created_at).
    occurred_at = models.DateTimeField(db_index=True)

    title = models.CharField(max_length=255, blank=True)
    subtitle = models.CharField(max_length=255, blank=True)
    badge_text = models.CharField(max_length=120, blank=True)
    badge_tone = models.CharField(
        max_length=10, choices=TimelineBadgeTone.choices,
        default=TimelineBadgeTone.NEUTRAL, blank=True,
    )

    # Attribution (mirrors api.history.ChangeSource): import | extension | admin | crm | system.
    source = models.CharField(max_length=20, blank=True, db_index=True)
    actor = models.CharField(max_length=120, blank=True)

    # Generic link to the source entity. object_id is a CharField because the
    # linked PKs are mixed (UUIDs for Screening/Assessment/Case/Client, ints for
    # Enrollment/EnrollmentProcess).
    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    object_id = models.CharField(max_length=64, blank=True)
    entity = GenericForeignKey("content_type", "object_id")

    metadata = models.JSONField(default=dict, blank=True)
    # Idempotency key, e.g. "screening:<uuid>" or "consent_granted:<client_id>".
    # Unique when set so emit() can update_or_create without duplicating.
    dedupe_key = models.CharField(max_length=128, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="unique_timeline_dedupe_key",
            )
        ]
        indexes = [
            models.Index(fields=["client", "occurred_at"]),
            models.Index(fields=["event_type"]),
            models.Index(fields=["enrollment", "renewal_number"]),
        ]

    def __str__(self):
        return f"{self.event_type} for {self.client_id} @ {self.occurred_at:%Y-%m-%d}"


# ---------------------------------------------------------------------------
# Menus & dietary tagging
# ---------------------------------------------------------------------------
class DietaryTagType(models.TextChoices):
    RESTRICTION = "restriction", "Restriction"
    ALLERGY = "allergy", "Allergy"


class DietaryTag(models.Model):
    """A single dietary tag, e.g. "no meat" (RESTRICTION) or "dairy" (ALLERGY)."""

    dietary_tag_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=120, unique=True)
    type = models.CharField(max_length=20, choices=DietaryTagType.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["type"])]

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class MenuType(models.Model):
    """A menu variant, e.g. Standard, Vegetarian, Dairy-Free. Associated with
    zero or more DietaryTags through MenuTypeTag."""

    menu_type_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=120, unique=True)
    # Whether this menu type is offered for new enrollments. Disabled types are
    # hidden from selection but kept for historical records.
    is_active = models.BooleanField(default=True)
    tags = models.ManyToManyField(
        DietaryTag,
        through="MenuTypeTag",
        related_name="menu_types",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class MenuTypeTag(models.Model):
    """Join table linking a MenuType to a DietaryTag."""

    menu_type = models.ForeignKey(
        MenuType, on_delete=models.CASCADE, related_name="menu_type_tags"
    )
    dietary_tag = models.ForeignKey(
        DietaryTag, on_delete=models.CASCADE, related_name="menu_type_tags"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["menu_type", "dietary_tag"],
                name="uniq_menu_type_dietary_tag",
            )
        ]
        indexes = [
            models.Index(fields=["menu_type"]),
            models.Index(fields=["dietary_tag"]),
        ]

    def __str__(self):
        return f"{self.menu_type} - {self.dietary_tag}"


# ---------------------------------------------------------------------------
# Kitchens
# ---------------------------------------------------------------------------
class KitchenStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    SUSPENDED = "suspended", "Suspended"


class KitchenIntegrationMethod(models.TextChoices):
    EMAIL = "email", "Email"
    API = "api", "API"


class KitchenProductType(models.TextChoices):
    """Kinds of product a kitchen can fulfill. A kitchen may support both."""

    MEAL = "meal", "Meal"
    BOX = "box", "Box"


class Kitchen(models.Model):
    """A kitchen/vendor that fulfills meal orders. Offers one or more
    MenuTypes (through KitchenMenuType) and is reached via one or more
    KitchenIntegrations (email or API)."""

    kitchen_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=KitchenStatus.choices,
        default=KitchenStatus.ACTIVE,
    )
    max_orders_per_day = models.PositiveIntegerField(null=True, blank=True)
    # Product kinds this kitchen supports, e.g. ["meal", "box"]. Values are
    # KitchenProductType codes; an empty list means none configured yet.
    supported_products = models.JSONField(default=list, blank=True)
    menu_types = models.ManyToManyField(
        MenuType,
        through="KitchenMenuType",
        related_name="kitchens",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return self.name


class KitchenMenuType(models.Model):
    """Join table: which MenuTypes a Kitchen offers, with the per-kitchen price
    and the allergies (DietaryTags) this kitchen CANNOT accommodate."""

    kitchen = models.ForeignKey(
        Kitchen, on_delete=models.CASCADE, related_name="kitchen_menu_types"
    )
    menu_type = models.ForeignKey(
        MenuType, on_delete=models.CASCADE, related_name="kitchen_menu_types"
    )
    # Price the kitchen charges for this menu type.
    menu_type_price = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    # Allergy DietaryTags this kitchen cannot manage for this menu type.
    # Members with any of these allergies must be routed to another kitchen.
    restrictions = models.ManyToManyField(
        DietaryTag,
        related_name="kitchen_menu_type_restrictions",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kitchen", "menu_type"],
                name="uniq_kitchen_menu_type",
            )
        ]
        indexes = [
            models.Index(fields=["kitchen"]),
            models.Index(fields=["menu_type"]),
        ]

    def __str__(self):
        return f"{self.kitchen} - {self.menu_type}"


class KitchenIntegration(models.Model):
    """How a Kitchen receives orders: by EMAIL or API. ``config`` carries the
    method-specific settings (endpoint, email, credentials, etc.)."""

    kitchen_integration_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    kitchen = models.ForeignKey(
        Kitchen, on_delete=models.CASCADE, related_name="integrations"
    )
    method = models.CharField(
        max_length=20, choices=KitchenIntegrationMethod.choices
    )
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kitchen", "method"]
        constraints = [
            models.UniqueConstraint(
                fields=["kitchen", "method"],
                name="uniq_kitchen_integration_method",
            )
        ]
        indexes = [models.Index(fields=["kitchen"])]

    def __str__(self):
        return f"{self.kitchen} ({self.get_method_display()})"


# ---------------------------------------------------------------------------
# Delivery companies
# ---------------------------------------------------------------------------
class DeliveryCompanyStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    SUSPENDED = "suspended", "Suspended"


class DeliveryCompanyIntegrationMethod(models.TextChoices):
    EMAIL = "email", "Email"
    API = "api", "API"


class DeliveryCompany(models.Model):
    """A delivery company/vendor that transports meal orders."""

    delivery_company_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=DeliveryCompanyStatus.choices,
        default=DeliveryCompanyStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "delivery companies"
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return self.name


class DeliveryCompanyIntegration(models.Model):
    """How a DeliveryCompany receives orders: by EMAIL or API. ``config``
    carries the method-specific settings (endpoint, email, credentials, etc.).
    At most one integration per company may be flagged ``is_primary``."""

    delivery_company_integration_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    delivery_company = models.ForeignKey(
        DeliveryCompany, on_delete=models.CASCADE, related_name="integrations"
    )
    method = models.CharField(
        max_length=20, choices=DeliveryCompanyIntegrationMethod.choices
    )
    is_primary = models.BooleanField(default=False)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["delivery_company", "-is_primary", "method"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_company", "method"],
                name="uniq_delivery_company_integration_method",
            ),
            # At most one primary integration per delivery company.
            models.UniqueConstraint(
                fields=["delivery_company"],
                condition=models.Q(is_primary=True),
                name="uniq_primary_delivery_company_integration",
            ),
        ]
        indexes = [models.Index(fields=["delivery_company"])]

    def __str__(self):
        return f"{self.delivery_company} ({self.get_method_display()})"


# ---------------------------------------------------------------------------
# Purchase orders & delivery orders
# ---------------------------------------------------------------------------
class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    CONFIRMED = "confirmed", "Confirmed"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class PurchaseOrderKitchenStatus(models.TextChoices):
    NOT_SENT = "not_sent", "Not Sent"
    SENT_TO_KITCHEN = "sent_to_kitchen", "Sent to Kitchen"
    ACCEPTED_BY_KITCHEN = "accepted_by_kitchen", "Accepted by Kitchen"
    IN_PREPARATION = "in_preparation", "In Preparation"
    READY_FOR_DISPATCH = "ready_for_dispatch", "Ready for Dispatch"


class PurchaseOrderDeliveryStatus(models.TextChoices):
    NOT_SENT = "not_sent", "Not Sent"
    SENT_TO_DELIVERY = "sent_to_delivery", "Sent to Delivery"
    ACCEPTED_BY_DELIVERY = "accepted_by_delivery", "Accepted by Delivery"
    OUT_FOR_DELIVERY = "out_for_delivery", "Out for Delivery"
    COMPLETED = "completed", "Completed"
    PARTIALLY_COMPLETED = "partially_completed", "Partially Completed"


class DeliveryOrderStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    READY_FOR_DELIVERY = "ready_for_delivery", "Ready for Delivery"
    OUT_FOR_DELIVERY = "out_for_delivery", "Out for Delivery"
    DELIVERED = "delivered", "Delivered"
    ON_HOLD = "on_hold", "On Hold"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    RETURNED = "returned", "Returned"


class PurchaseOrder(models.Model):
    """A batch order placed with a Kitchen and routed through a DeliveryCompany.
    Contains many DeliveryOrders (one per member/household)."""

    purchase_order_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    # Human-readable, unique business key, e.g. "PO-MEALS-2026-W26-THU-K01" or
    # "PO-BOX-2026-W26-K01". The UUID above stays the real primary key.
    po_number = models.CharField(max_length=64, blank=True, unique=True, null=True)
    # Which product this PO is for. ``kind`` is the coarse Meals/Boxes split;
    # ``product_type`` pins the exact ProductType (kind + cadence).
    kind = models.CharField(
        max_length=20, choices=ProductTypeKind.choices, blank=True
    )
    product_type = models.ForeignKey(
        ProductType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchase_orders",
    )
    # Planned fulfillment (delivery) date.
    delivery_date = models.DateField(null=True, blank=True)
    # The PO/cutoff date the order is placed on (distinct from delivery_date),
    # derived from the product's PO->delivery weekday schedule.
    po_date = models.DateField(null=True, blank=True)
    # When this PO was split off another (e.g. a kitchen could only fulfil part
    # of the batch on the original date). Null for originally-generated POs.
    split_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="splits",
    )
    status = models.CharField(
        max_length=20,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
    )
    kitchen_status = models.CharField(
        max_length=30,
        choices=PurchaseOrderKitchenStatus.choices,
        default=PurchaseOrderKitchenStatus.NOT_SENT,
    )
    delivery_status = models.CharField(
        max_length=30,
        choices=PurchaseOrderDeliveryStatus.choices,
        default=PurchaseOrderDeliveryStatus.NOT_SENT,
    )
    kitchen = models.ForeignKey(
        Kitchen,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders",
    )
    delivery_company = models.ForeignKey(
        DeliveryCompany,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders",
    )
    # Timestamps for when the order was dispatched to each party.
    sent_to_kitchen_at = models.DateTimeField(null=True, blank=True)
    sent_to_delivery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["delivery_date"]),
            models.Index(fields=["kitchen", "status"]),
        ]

    def __str__(self):
        return f"PO {self.purchase_order_id} ({self.get_status_display()})"


class DeliveryOrder(models.Model):
    """A single member's delivery within a PurchaseOrder."""

    delivery_order_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="delivery_orders"
    )
    member = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
    )
    group = models.ForeignKey(
        Household,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
    )
    status = models.CharField(
        max_length=30,
        choices=DeliveryOrderStatus.choices,
        default=DeliveryOrderStatus.PENDING,
    )
    # How many meals/boxes this member receives for this delivery. Snapshotted
    # from OrderSchedule.how_many_meals_or_boxes at PO generation so the kitchen
    # export reflects the quantity even if the schedule later changes.
    quantity = models.PositiveSmallIntegerField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # The ACTUAL kitchen fulfilling this delivery. Defaults to the household's
    # assigned kitchen but may be rerouted at PO time (load balancing) for this
    # delivery only — the household preference is never mutated.
    kitchen = models.ForeignKey(
        Kitchen,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
    )
    # The household's default/assigned kitchen at PO time, kept so the UI can
    # show "rerouted from X". Equals ``kitchen`` unless rerouted.
    default_kitchen = models.ForeignKey(
        Kitchen,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_delivery_orders",
    )
    # True when ``kitchen`` differs from ``default_kitchen`` (load-balanced to a
    # different capable kitchen for this delivery).
    rerouted = models.BooleanField(default=False)
    menu_type = models.ForeignKey(
        MenuType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
    )
    # Per-member meal-rule result, snapshotted at PO generation. These are what
    # the kitchen export sends for each member (replacing the raw menu type +
    # derived food note). ``kitchen_meal_type`` may be a catalog MenuType name
    # or "Allergen Free".
    kitchen_meal_type = models.CharField(max_length=120, blank=True)
    kitchen_food_notes = models.TextField(blank=True)
    # Per-order overrides on top of the MenuType's tags.
    custom_dietary_tags = models.ManyToManyField(
        DietaryTag, related_name="delivery_orders", blank=True
    )
    delivery_company = models.ForeignKey(
        DeliveryCompany,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
    )
    # Proof of delivery: list of image references (URLs / storage keys).
    proof_of_delivery = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["purchase_order"]),
            models.Index(fields=["status"]),
            models.Index(fields=["member"]),
            models.Index(fields=["expected_delivery_date"]),
        ]

    def __str__(self):
        return f"DeliveryOrder {self.delivery_order_id} ({self.get_status_display()})"


# ===========================================================================
# TICKET NOTES (customer-support portal)
# ===========================================================================
class TicketNote(models.Model):
    """A customer-support note added to a Ticket from the support portal.

    Distinct from :class:`Note` (Unite Us client/case notes): these are
    internal agent actions documenting work on a follow-up ticket.
    """

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="notes"
    )
    # The agent who wrote the note; ``author_name`` is a snapshot so the row
    # stays readable even if the agent record changes/clears.
    author_agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ticket_notes",
    )
    author_name = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["ticket", "created_at"])]

    def __str__(self):
        return f"Note on ticket {self.ticket_id} by {self.author_name or 'system'}"


# ===========================================================================
# LEADS (public eligibility funnel)
# ===========================================================================
class Lead(models.Model):
    """A prospective member captured from the public eligibility funnel
    (Benefully mobile app / landing page).

    Two-step intake:
      * Step 1 (required): contact fields + Medicaid enrollment status + the
        legal disclaimer acceptance (TCPA-style consent to be contacted).
      * Step 2 (optional): enrichment fields filled in on a follow-up screen.

    The disclaimer acceptance timestamp (``disclaimer_accepted_at``) is stamped
    automatically when consent is recorded. ``do_not_contact`` is an explicit
    opt-out the lead can set to stop further outreach.
    """

    class MedicaidEnrollment(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        NOT_SURE = "not_sure", "Not sure"

    class ContactMethod(models.TextChoices):
        PHONE = "phone", "Phone"
        TEXT = "text", "Text"
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        NEW = "new", "New"
        ATTEMPTING_CONTACT = "attempting_contact", "Attempting to Contact"
        CONTACTED = "contacted", "Contacted"
        ENROLLED = "enrolled", "Enrolled"
        NOT_ELIGIBLE = "not_eligible", "Not Eligible"
        CLOSED = "closed", "Close"
        LOST = "lost", "Lost"

    lead_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Step 1: required capture ---
    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)
    # Phone is the primary follow-up channel; kept as free text (normalization
    # happens at the serializer layer) so international/format variants survive.
    phone_number = models.CharField(max_length=32, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    zip_code = models.CharField(max_length=10)
    medicaid_enrollment = models.CharField(
        max_length=10, choices=MedicaidEnrollment.choices
    )

    # Legal disclaimer acceptance — consent to be contacted by phone/text/email.
    disclaimer_accepted = models.BooleanField(default=False)
    disclaimer_accepted_at = models.DateTimeField(null=True, blank=True)

    # --- Step 2: optional enrichment ---
    medicaid_id = models.CharField(max_length=60, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    additional_details = models.TextField(blank=True)
    household_size = models.PositiveSmallIntegerField(null=True, blank=True)
    preferred_contact_method = models.CharField(
        max_length=10, choices=ContactMethod.choices, blank=True
    )

    # Explicit opt-out: when true, do not contact the lead any further.
    do_not_contact = models.BooleanField(default=False)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )

    # --- Assignment & conversion tracking ---
    # Screener responsible for following up on this lead.
    assigned_to = models.ForeignKey(
        "Agent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_leads",
    )
    # Set when a lead converts into an enrolled client, for funnel tracking.
    converted_client = models.ForeignKey(
        "Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads",
    )
    # Program main categories the lead expressed interest in (multi-select).
    interested_programs = models.ManyToManyField(
        "ProgramMainCategory",
        blank=True,
        related_name="interested_leads",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Lead {self.first_name} {self.last_name} ({self.phone_number})"


class LeadNote(models.Model):
    """A follow-up note an agent records against a :class:`Lead`.

    Author is stored both as a nullable FK (so we can keep the link even if the
    note text is the source of truth) and as a denormalized ``author_name`` so
    the display name survives if the agent is later removed.
    """

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        "Agent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lead_notes",
    )
    author_name = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note on {self.lead_id} by {self.author_name or 'agent'}"
