import uuid

from django.conf import settings
from django.db import models


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
    """Acquisition funnel stage for a client/person. Auto-derived from synced
    Unite Us data (consent, screenings, eligibility, cases) by
    ``api.services.lifecycle.recompute_client_stage``.

    lead -> prospect -> screened -> eligible | ineligible -> client
    """

    LEAD = "lead", "Lead"  # pulled from Unite Us (Medicaid), no consent yet
    PROSPECT = "prospect", "Prospect"  # consent accepted
    SCREENED = "screened", "Screened"  # >=1 completed Met Council screening
    ELIGIBLE = "eligible", "Eligible"  # eligibility assessment found eligible
    INELIGIBLE = "ineligible", "Ineligible"  # eligibility found not eligible
    CLIENT = "client", "Client"  # >=1 Met Council case exists


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
    last_name = models.CharField(max_length=120)
    date_of_birth = models.DateField(null=True, blank=True)  # PII
    client_phone_number = models.CharField(max_length=30, blank=True)  # PII
    phone_type = models.CharField(max_length=20, blank=True)  # mobile/home/work
    client_email_address = models.EmailField(blank=True)  # PII
    consent_accepted = models.BooleanField(default=False)
    consent_status = models.CharField(max_length=20, blank=True)  # E-form: accepted/declined
    consented_at = models.DateTimeField(null=True, blank=True)
    consent_doc_url = models.URLField(blank=True)

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
    household_size = models.PositiveSmallIntegerField(null=True, blank=True)
    household_income_range = models.CharField(max_length=2, blank=True)  # Enum

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
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=2, blank=True)
    zip = models.CharField(max_length=10, blank=True)
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


class Insurance(models.Model):
    """Normalized insurance record. A client may have multiple plans over time."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="insurances"
    )
    plan_external_id = models.CharField(max_length=64, blank=True, db_index=True)
    plan_type = models.CharField(
        max_length=20, choices=InsurancePlanType.choices, blank=True
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

    class Meta:
        ordering = ["-is_primary", "-enrolled_at"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["insurance_id"]),
        ]

    def __str__(self):
        return f"{self.plan_name or self.plan_type} for {self.client_id}"


# ===========================================================================
# PRODUCT & SERVICE CATALOG
# ===========================================================================
class Product(models.Model):
    """A product we can deliver to a client. Products are derived from the
    screening result. We track every product available in Unite Us, but only
    flag the ones we actually offer (``is_offered``). Today the only offered
    product is Food.
    """

    code = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # We provide this product (vs. merely tracking that it exists in Unite Us).
    is_offered = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    source = models.CharField(max_length=120, default="Unite Us")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["is_offered", "is_active"])]

    def __str__(self):
        return self.name


class Service(models.Model):
    """A service type a client may be eligible for, taken from the eligibility
    result ("Client May Be Eligible for:"). Mirrors the Unite Us taxonomy
    (``ServiceType``). We keep every value, but only flag the ones we offer
    (``is_offered``); offered services map to the ``Product`` we deliver them
    under. Currently offered: Medically Tailored Meals (MTM) and Clinically
    Appropriate Meals, both under the Food product.
    """

    code = models.CharField(
        max_length=80, choices=ServiceType.choices, unique=True
    )
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=120, blank=True)  # e.g. "Food"
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
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
            models.Index(fields=["product"]),
        ]

    def __str__(self):
        return self.name


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


class Program(models.Model):
    """Normalized program offered by a provider."""

    program_id = models.UUIDField(primary_key=True, editable=False)
    name = models.CharField(max_length=255)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


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
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contracted_services",
    )

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


class AnswerType(models.TextChoices):
    TEXT = "text", "Text"
    BOOLEAN = "boolean", "Boolean"
    DATE = "date", "Date"
    DATETIME = "datetime", "Datetime"
    INTEGER = "integer", "Integer"
    FLOAT = "float", "Float"
    SINGLE_SELECT = "single_select", "Single Select"
    MULTI_SELECT = "multi_select", "Multi Select"


class ScreenTemplate(models.Model):
    """Normalized screening template (questionnaire definition)."""

    template_id = models.UUIDField(primary_key=True, editable=False)
    active_template = models.BooleanField(default=True)
    template_description = models.TextField(blank=True)
    template_hcpcs_code = models.CharField(max_length=20, blank=True)
    template_loinc_code = models.CharField(max_length=20, blank=True)
    template_loinc_group = models.CharField(max_length=50, blank=True)
    template_loinc_version = models.CharField(max_length=20, blank=True)
    parent_template = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="child_templates",
    )
    template_snomed_codes = models.JSONField(default=list, blank=True)
    template_source = models.CharField(max_length=120, blank=True)
    template_status = models.CharField(max_length=50, blank=True)
    template_status_at = models.DateTimeField(null=True, blank=True)
    template_title = models.CharField(max_length=255, blank=True)
    template_type = models.CharField(max_length=80, blank=True)
    template_version = models.CharField(max_length=20, blank=True)
    from_file = models.BooleanField(default=False)

    class Meta:
        ordering = ["template_title"]

    def __str__(self):
        return self.template_title or str(self.template_id)


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


class Eligibility(models.Model):
    """An eligibility assessment for a subject (client)."""

    # --- Core Information ---
    eligibility_id = models.UUIDField(primary_key=True, editable=False)
    subject_id = models.UUIDField(db_index=True)  # source client reference
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="eligibilities",
    )
    screen_created_at = models.DateTimeField(null=True, blank=True)
    eligible_status = models.CharField(max_length=50, blank=True)  # e.g., "Eligible", "Not Eligible"

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
        verbose_name_plural = "eligibilities"
        indexes = [
            models.Index(fields=["subject_id"]),
            models.Index(fields=["client", "eligible_status"]),
        ]

    def __str__(self):
        return f"Eligibility {self.eligibility_id} ({self.eligible_status})"


class Question(models.Model):
    """Normalized screening question (belongs to a template)."""

    question_id = models.UUIDField(primary_key=True, editable=False)
    template = models.ForeignKey(
        ScreenTemplate, on_delete=models.CASCADE, null=True, blank=True,
        related_name="questions",
    )
    parent_question = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="child_questions",
    )
    question_primary_text = models.TextField(blank=True)
    question_secondary_text = models.TextField(blank=True)
    question_required = models.BooleanField(default=False)
    question_type = models.CharField(max_length=50, blank=True)
    question_category = models.CharField(max_length=120, blank=True)
    question_language = models.CharField(max_length=80, blank=True)
    question_loinc_code = models.CharField(max_length=20, blank=True)
    question_loinc_version = models.CharField(max_length=20, blank=True)
    question_hcpcs_code = models.CharField(max_length=20, blank=True)
    question_status = models.CharField(max_length=50, blank=True)
    question_status_at = models.DateTimeField(null=True, blank=True)
    question_is_active = models.BooleanField(default=True)
    admin_only = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["template"])]

    def __str__(self):
        return self.question_primary_text[:60] or str(self.question_id)


class QuestionOption(models.Model):
    """Normalized answer option for a question."""

    question_option_id = models.UUIDField(primary_key=True, editable=False)
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, null=True, blank=True,
        related_name="options",
    )
    parent_question_option = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="child_options",
    )
    question_option_text = models.TextField(blank=True)
    question_option_category_code = models.CharField(max_length=80, blank=True)
    question_option_loinc_list_id = models.CharField(max_length=50, blank=True)
    question_option_loinc_version = models.CharField(max_length=20, blank=True)
    question_option_weight = models.FloatField(null=True, blank=True)
    question_option_language = models.CharField(max_length=80, blank=True)
    question_option_hcpcs_code = models.CharField(max_length=20, blank=True)
    question_option_loinc_code = models.CharField(max_length=20, blank=True)
    question_option_icd10_codes = models.JSONField(default=list, blank=True)
    question_option_snomed_codes = models.JSONField(default=list, blank=True)
    question_option_score = models.FloatField(null=True, blank=True)
    question_option_type = models.CharField(max_length=50, blank=True)
    question_option_value = models.CharField(max_length=255, blank=True)
    question_option_value_bool = models.BooleanField(null=True, blank=True)
    question_option_value_float = models.FloatField(null=True, blank=True)
    question_option_value_int = models.IntegerField(null=True, blank=True)
    question_option_is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["question"])]

    def __str__(self):
        return self.question_option_text[:60] or str(self.question_option_id)


class Answer(models.Model):
    """A client's answer to a question within a screening or eligibility assessment."""

    answer_id = models.UUIDField(primary_key=True, editable=False)
    # An answer belongs to EITHER a screening or an eligibility assessment.
    screening = models.ForeignKey(
        Screening, on_delete=models.CASCADE, null=True, blank=True,
        related_name="answers",
    )
    eligibility = models.ForeignKey(
        Eligibility, on_delete=models.CASCADE, null=True, blank=True,
        related_name="answers",
    )
    question = models.ForeignKey(
        Question, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="answers",
    )
    question_option = models.ForeignKey(
        "QuestionOption", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="answers",
    )  # the selected option (source question_option_id)
    answer_is_active = models.BooleanField(default=True)
    answer_language = models.CharField(max_length=80, blank=True)
    answer_type = models.CharField(
        max_length=20, choices=AnswerType.choices, blank=True
    )
    answer_status = models.CharField(max_length=50, blank=True)
    answer_status_at = models.DateTimeField(null=True, blank=True)
    answer_value = models.TextField(blank=True)  # PHI
    answer_value_bool = models.BooleanField(null=True, blank=True)
    answer_value_datetime = models.DateTimeField(null=True, blank=True)
    answer_value_float = models.FloatField(null=True, blank=True)
    answer_value_int = models.IntegerField(null=True, blank=True)
    value_string = models.TextField(blank=True)  # PHI
    answer_score = models.FloatField(null=True, blank=True)
    answer_weight = models.FloatField(null=True, blank=True)
    interpretations = models.JSONField(default=list, blank=True)
    answer_created_at = models.DateTimeField(null=True, blank=True)
    answer_updated_at = models.DateTimeField(null=True, blank=True)
    translated_by_id = models.UUIDField(null=True, blank=True)
    translated_by_type = models.CharField(max_length=50, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["screening"]),
            models.Index(fields=["eligibility"]),
            models.Index(fields=["question"]),
        ]

    def __str__(self):
        return f"Answer {self.answer_id}"


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
# IMPORT / ETL METADATA
# ===========================================================================
class ImportSource(models.TextChoices):
    CLIENTS = "clients", "Clients"
    CASES = "cases", "Cases"
    SCREENINGS = "screenings", "Screenings"


class ImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ImportBatch(models.Model):
    """One CSV import run. Normalizes the per-row ETL metadata (pull_* / data_pulled_at)
    that repeats across every source row, and anchors the admin import feature."""

    source = models.CharField(max_length=20, choices=ImportSource.choices)
    file_name = models.CharField(max_length=255, blank=True)
    pull_start_date = models.DateField(null=True, blank=True)
    pull_end_date = models.DateField(null=True, blank=True)
    pull_timestamp = models.DateTimeField(null=True, blank=True)
    data_pulled_at = models.DateTimeField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=ImportStatus.choices, default=ImportStatus.PENDING
    )
    error_log = models.TextField(blank=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="import_batches",
    )
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]
        indexes = [models.Index(fields=["source", "status"])]

    def __str__(self):
        return f"{self.get_source_display()} import {self.pk} ({self.status})"



class ScreeningForm(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Questionnaire(models.Model):
    screening = models.OneToOneField(
        ScreeningForm,
        on_delete=models.CASCADE,
        related_name="screening_questionnaire"
    )
    title = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.title} ({self.screening.name})"


class Assessment(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class AssessmentQuestionnaire(models.Model):
    assessment = models.OneToOneField(
        Assessment,
        on_delete=models.CASCADE,
        related_name="assessment_questionnaire"
    )
    title = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.title} ({self.assessment.name})"


# ===========================================================================
# CLIENT LIFECYCLE / ENROLLMENT
# ===========================================================================
# The acquisition funnel (Client.lifecycle_stage) is auto-derived from synced
# Unite Us data. Service delivery is tracked per-product on an Enrollment, which
# advances through its own stages via guarded manual transitions. StageEvent is
# an append-only audit log of every transition (powers funnel/time-in-stage
# reporting). See api.services.lifecycle for the transition logic.
class EnrollmentStage(models.TextChoices):
    """Service-delivery stage for a single (client, product) enrollment."""

    PENDING_VALIDATION = "pending_validation", "Pending Validation"
    VALIDATED = "validated", "Validated"
    PENDING_VERIFICATION = "pending_verification", "Pending Verification"
    VERIFIED = "verified", "Verified"
    SERVICE_ACTIVE = "service_active", "Service Active"
    SERVICE_COMPLETE = "service_complete", "Service Complete"
    CLOSED = "closed", "Closed"
    ON_HOLD = "on_hold", "On Hold"
    CANCELLED = "cancelled", "Cancelled"


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


class Enrollment(models.Model):
    """A client's enrollment into a single product we deliver. One row per
    (client, product). Owns the service-delivery stage and its schedule.

    The acquisition funnel lives on Client.lifecycle_stage; this picks up once a
    client reaches the ``client`` stage and we begin validation/verification/
    service for a specific product.
    """

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="enrollments"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="enrollments"
    )
    # The Met Council case this enrollment is delivered under (optional until a
    # case exists).
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollments",
    )
    stage = models.CharField(
        max_length=25, choices=EnrollmentStage.choices,
        default=EnrollmentStage.PENDING_VALIDATION, db_index=True,
    )
    stage_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "product"], name="unique_client_product_enrollment"
            )
        ]
        indexes = [
            models.Index(fields=["client", "stage"]),
            models.Index(fields=["product", "stage"]),
        ]

    def __str__(self):
        return f"{self.client_id} / {self.product_id} ({self.stage})"


class EnrollmentProcess(models.Model):
    """A validation/verification (or future) process run for an enrollment.

    Process-specific captured data (validated delivery address, household size,
    verification answers, etc.) is stored in ``data`` as flexible JSON; canonical
    values still live on Client/Address. When the verification questionnaire
    firms up, ``data`` can graduate into structured question/answer tables.
    """

    enrollment = models.ForeignKey(
        Enrollment, on_delete=models.CASCADE, related_name="processes"
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
        Enrollment, on_delete=models.CASCADE, related_name="schedules"
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
        Enrollment, on_delete=models.CASCADE, null=True, blank=True,
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
        ("Management", "Management"),
        ("CS", "CS"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    agent_code = models.CharField(max_length=20, unique=True, db_index=True)
    group = models.CharField(max_length=50, choices=AGENT_GROUPS, default="Screeners")
    status = models.CharField(max_length=20, default="Active")
    cbo = models.CharField(max_length=255, blank=True, default="Met Council")
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
