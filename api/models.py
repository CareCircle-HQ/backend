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
    MAILING = "mailing", "Mailing"
    DELIVERY = "delivery", "Delivery"


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

    # --- Core Client Info ---
    # Primary key is the source system's external_id (UUID string).
    client_id = models.UUIDField(primary_key=True, editable=False)
    created_by_id = models.UUIDField(null=True, blank=True)  # source agent id
    created_by_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)  # source creation
    updated_at = models.DateTimeField(null=True, blank=True)  # source last update
    is_active = models.BooleanField(default=True)
    crm_contact_id = models.CharField(max_length=64, blank=True, db_index=True)
    last_synced_at = models.DateTimeField(auto_now=True)  # local ingest tracking
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="clients",
    )

    # --- Personal Information ---
    first_name = models.CharField(max_length=120)
    middle_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120)
    suffix = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=20, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)  # PII
    gender = models.CharField(
        max_length=20, choices=Gender.choices, blank=True
    )
    sexuality = models.CharField(max_length=50, blank=True)
    sexuality_other = models.CharField(max_length=120, blank=True)
    race = models.CharField(max_length=100, blank=True)
    ethnicity = models.CharField(max_length=100, blank=True)
    marital_status = models.CharField(
        max_length=20, choices=MaritalStatus.choices, blank=True
    )
    citizenship = models.CharField(max_length=100, blank=True)
    time_zone = models.CharField(max_length=64, default="America/New_York")
    enrollment_from = models.CharField(max_length=120, default="Unite Us")
    lead_source = models.CharField(max_length=120, blank=True)

    # --- Program Eligibility & Referral (multi-select) ---
    # Lists of ServiceType values; validated in the serializer.
    eligible_for = models.JSONField(default=list, blank=True)
    referred_for = models.JSONField(default=list, blank=True)

    # --- Family / Household flags ---
    is_family = models.BooleanField(default=False)
    total_family_members = models.PositiveIntegerField(
        null=True, blank=True
    )  # includes the primary client

    # --- Attestation & Delivery ---
    attestation_needed = models.BooleanField(default=False)
    different_delivery_address = models.BooleanField(default=False)

    # --- Agent / Call Tracking ---
    agent_code = models.CharField(max_length=64, blank=True, db_index=True)
    call_duration_minutes = models.PositiveIntegerField(
        null=True, blank=True
    )  # length of the eligibility phone call, in minutes
    call_transfer_answered = models.CharField(
        max_length=30, choices=CallTransferStatus.choices, blank=True
    )

    # --- Consent ---
    consent_status = models.CharField(
        max_length=20, choices=ConsentStatus.choices, default=ConsentStatus.PENDING
    )
    consented_at = models.DateTimeField(null=True, blank=True)

    # --- Household & Income ---
    gross_monthly_income = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    household_size = models.PositiveIntegerField(null=True, blank=True)
    adults_in_household = models.PositiveIntegerField(null=True, blank=True)
    children_in_household = models.PositiveIntegerField(null=True, blank=True)

    # --- Communication Preferences ---
    preferred_communication_method = models.CharField(
        max_length=20, choices=CommunicationChannel.choices, blank=True
    )
    preferred_communication_time_of_day = models.JSONField(
        default=default_communication_time_of_day, blank=True
    )
    preferred_spoken_language = models.CharField(max_length=80, blank=True)
    preferred_written_language = models.CharField(max_length=80, blank=True)
    communication_channel = models.CharField(
        max_length=20, choices=CommunicationChannel.choices, blank=True
    )

    # --- Contact Information (primary) ---
    phone_type = models.CharField(
        max_length=10, choices=PhoneType.choices, default=PhoneType.MOBILE, blank=True
    )
    client_phone_number = models.CharField(max_length=32, blank=True)  # PII
    client_email_address = models.EmailField(blank=True)  # PII

    # --- Care Coordination ---
    care_coordinator = models.CharField(max_length=255, blank=True)
    care_coordinator_status = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["last_name", "first_name"]),
            models.Index(fields=["date_of_birth"]),
            models.Index(fields=["created_by_id"]),
            models.Index(fields=["is_active"]),
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
    """Normalized address. Supports current + mailing, with active/history tracking."""

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="addresses"
    )
    address_type = models.CharField(
        max_length=10, choices=AddressType.choices, default=AddressType.CURRENT
    )
    is_mailing_address = models.BooleanField(default=False)
    line1 = models.CharField(max_length=255, blank=True)  # PII
    line2 = models.CharField(max_length=255, blank=True)  # PII
    city = models.CharField(max_length=120, blank=True)
    county = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    state = models.CharField(max_length=2, choices=USState.choices, blank=True)
    is_active = models.BooleanField(default=True)
    added_by_name = models.CharField(max_length=255, blank=True)
    validated = models.BooleanField(default=False)
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_active", "address_type"]
        indexes = [
            models.Index(fields=["client", "address_type", "is_active"]),
            models.Index(fields=["postal_code"]),
        ]

    def __str__(self):
        return f"{self.get_address_type_display()} address for {self.client_id}"


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
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="cases"
    )
    # Denormalized client snapshot (as received at case creation).
    client_first_name = models.CharField(max_length=120, blank=True)
    client_last_name = models.CharField(max_length=120, blank=True)
    client_dob = models.DateField(null=True, blank=True)  # PII/PHI
    previous_case = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_cases",
    )
    created_by_id = models.UUIDField(null=True, blank=True)  # source agent id
    created_by_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)  # source creation
    updated_at = models.DateTimeField(null=True, blank=True)  # source last update

    # Product (model to be defined later) - placeholder reference for now.
    product_id = models.UUIDField(null=True, blank=True)

    # --- Case Dates & Timeline ---
    user_entered_opened_date = models.DateField(null=True, blank=True)
    user_entered_closed_date = models.DateField(null=True, blank=True)
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
    program_name = models.CharField(max_length=255, blank=True)

    # --- Case Assignment ---
    primary_worker_id = models.UUIDField(null=True, blank=True)
    primary_worker_name = models.CharField(max_length=255, blank=True)
    care_coordinator = models.CharField(max_length=255, blank=True)
    care_coordinator_status = models.CharField(max_length=80, blank=True)

    # --- Service Information ---
    # service_type/subtype come from the Unite Us taxonomy (180+ values); the
    # upcoming Product/Service model will own the canonical list, so kept as
    # indexed text here rather than a fixed enum.
    service_type = models.CharField(max_length=120, blank=True, db_index=True)
    service_subtype = models.CharField(max_length=120, blank=True)

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
    service_authorization_request_starts_at = models.DateTimeField(null=True, blank=True)
    service_authorization_request_ends_at = models.DateTimeField(null=True, blank=True)
    service_authorization_approval_starts_at = models.DateTimeField(null=True, blank=True)
    service_authorization_approval_ends_at = models.DateTimeField(null=True, blank=True)

    # --- Export Metadata ---
    export_provider_role = models.CharField(max_length=80, blank=True)
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cases",
    )

    class Meta:
        ordering = ["-created_at"]
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
    # subject_id is the source's client reference; mapped to `client` FK on import.
    subject_id = models.UUIDField(db_index=True)
    subject_type = models.CharField(max_length=50, blank=True)
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="screenings",
    )  # mapped from subject_id during import
    active_screen = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    assigned_to_id = models.UUIDField(null=True, blank=True)
    screen_created_at = models.DateTimeField(null=True, blank=True)
    screen_updated_at = models.DateTimeField(null=True, blank=True)
    screen_status = models.CharField(
        max_length=20, choices=ScreenStatus.choices, blank=True
    )
    screen_status_at = models.DateTimeField(null=True, blank=True)
    screen_type = models.CharField(
        max_length=20, choices=ScreenType.choices, blank=True
    )
    screen_source = models.CharField(max_length=120, blank=True)
    parent_screen = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="child_screens",
    )
    related_screen = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="related_to_screens",
    )
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="screenings",
    )
    template = models.ForeignKey(
        ScreenTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="screenings",
    )

    # --- Client Snapshot ---
    client_first_name = models.CharField(max_length=120, blank=True)
    client_last_name = models.CharField(max_length=120, blank=True)
    client_dob = models.DateField(null=True, blank=True)  # PII/PHI

    # --- Screening Timing & Activity ---
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    facilitator_id = models.UUIDField(null=True, blank=True)
    facilitator_type = models.CharField(max_length=50, blank=True)
    provider_id = models.UUIDField(null=True, blank=True)
    provider_name = models.CharField(max_length=255, blank=True)
    performing_organization_name = models.CharField(max_length=255, blank=True)
    outreach_count = models.PositiveIntegerField(default=0)
    outreach_status = models.CharField(
        max_length=20, choices=OutreachStatus.choices, blank=True
    )

    # --- Decline / Outreach Details ---
    decline_note = models.TextField(blank=True)
    decline_reason_id = models.UUIDField(null=True, blank=True)
    decline_primary_text = models.CharField(max_length=255, blank=True)
    decline_secondary_text = models.CharField(max_length=255, blank=True)
    decline_reason_key = models.CharField(max_length=120, blank=True)

    # --- Communication & Interpreter ---
    interpreter_id = models.UUIDField(null=True, blank=True)
    interpreter_type = models.CharField(max_length=50, blank=True)
    language = models.CharField(max_length=80, blank=True)

    # --- Consent & Risk Scoring ---
    consent = models.BooleanField(null=True, blank=True)
    consent_code = models.CharField(max_length=80, blank=True)
    interpersonal_safety_riskscore = models.FloatField(null=True, blank=True)  # PHI
    interpersonal_safety_interpretation = models.CharField(max_length=255, blank=True)

    # --- Clinical Coding ---
    screen_snomed_codes = models.JSONField(default=list, blank=True)
    screen_icd10_codes = models.JSONField(default=list, blank=True)
    clinical_code_classification = models.CharField(max_length=120, blank=True)
    verified_clinical_code = models.CharField(max_length=50, blank=True)
    verified_clinical_code_description = models.CharField(max_length=255, blank=True)

    # --- Eligibility ---
    eligible_status = models.CharField(max_length=50, blank=True)
    eligible_services = models.JSONField(default=list, blank=True)

    # --- Verification ---
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by_id = models.UUIDField(null=True, blank=True)
    verified_by_type = models.CharField(max_length=50, blank=True)

    # --- Sensitivity Flags ---
    is_case_sensitive = models.BooleanField(default=False)

    # --- Filtering & Metadata (ETL) ---
    filter_date = models.DateField(null=True, blank=True)
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="screenings",
    )

    class Meta:
        ordering = ["-screen_created_at"]
        indexes = [
            models.Index(fields=["subject_id"]),
            models.Index(fields=["client", "screen_status"]),
            models.Index(fields=["case"]),
            models.Index(fields=["screen_status"]),
            models.Index(fields=["screen_type"]),
            models.Index(fields=["template"]),
        ]

    def __str__(self):
        return f"Screening {self.enhanced_screen_id} ({self.screen_status})"


class Eligibility(models.Model):
    """An eligibility assessment for a subject (client).

    Structurally the same shape as a Screening, but stored separately because
    eligibility assessments are a distinct record type. Import routing uses the
    source ``screen_type`` to decide between Screening and Eligibility.
    """

    # source enhanced_screen_id of the assessment
    eligibility_id = models.UUIDField(primary_key=True, editable=False)
    subject_id = models.UUIDField(db_index=True)  # source client reference
    subject_type = models.CharField(max_length=50, blank=True)
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="eligibilities",
    )  # mapped from subject_id during import
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="eligibilities",
    )
    active_screen = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    assigned_to_id = models.UUIDField(null=True, blank=True)
    screen_created_at = models.DateTimeField(null=True, blank=True)
    screen_updated_at = models.DateTimeField(null=True, blank=True)
    screen_status = models.CharField(
        max_length=20, choices=ScreenStatus.choices, blank=True
    )
    screen_status_at = models.DateTimeField(null=True, blank=True)
    screen_type = models.CharField(
        max_length=20, choices=ScreenType.choices, blank=True
    )
    screen_source = models.CharField(max_length=120, blank=True)

    # --- Client Snapshot ---
    client_first_name = models.CharField(max_length=120, blank=True)
    client_last_name = models.CharField(max_length=120, blank=True)
    client_dob = models.DateField(null=True, blank=True)  # PII/PHI

    # --- Timing & Activity ---
    duration = models.PositiveIntegerField(null=True, blank=True)
    facilitator_id = models.UUIDField(null=True, blank=True)
    facilitator_type = models.CharField(max_length=50, blank=True)
    provider_id = models.UUIDField(null=True, blank=True)
    provider_name = models.CharField(max_length=255, blank=True)
    performing_organization_name = models.CharField(max_length=255, blank=True)
    outreach_count = models.PositiveIntegerField(default=0)
    outreach_status = models.CharField(
        max_length=20, choices=OutreachStatus.choices, blank=True
    )

    # --- Decline / Outreach ---
    decline_note = models.TextField(blank=True)
    decline_reason_id = models.UUIDField(null=True, blank=True)
    decline_primary_text = models.CharField(max_length=255, blank=True)
    decline_secondary_text = models.CharField(max_length=255, blank=True)
    decline_reason_key = models.CharField(max_length=120, blank=True)

    # --- Communication & Interpreter ---
    interpreter_id = models.UUIDField(null=True, blank=True)
    interpreter_type = models.CharField(max_length=50, blank=True)
    language = models.CharField(max_length=80, blank=True)

    # --- Consent & Risk Scoring ---
    consent = models.BooleanField(null=True, blank=True)
    consent_code = models.CharField(max_length=80, blank=True)
    interpersonal_safety_riskscore = models.FloatField(null=True, blank=True)  # PHI
    interpersonal_safety_interpretation = models.CharField(max_length=255, blank=True)

    # --- Clinical Coding ---
    screen_snomed_codes = models.JSONField(default=list, blank=True)
    screen_icd10_codes = models.JSONField(default=list, blank=True)
    clinical_code_classification = models.CharField(max_length=120, blank=True)
    verified_clinical_code = models.CharField(max_length=50, blank=True)
    verified_clinical_code_description = models.CharField(max_length=255, blank=True)

    # --- Eligibility ---
    eligible_status = models.CharField(max_length=50, blank=True)
    eligible_services = models.JSONField(default=list, blank=True)
    # Captured assessment question/answer pairs (the model has no normalized
    # Answer relation like Screening, so responses are stored inline).
    responses = models.JSONField(default=list, blank=True)

    # --- Verification ---
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by_id = models.UUIDField(null=True, blank=True)
    verified_by_type = models.CharField(max_length=50, blank=True)

    # --- Sensitivity & Metadata ---
    is_case_sensitive = models.BooleanField(default=False)
    filter_date = models.DateField(null=True, blank=True)
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="eligibilities",
    )

    class Meta:
        ordering = ["-screen_created_at"]
        verbose_name_plural = "eligibilities"
        indexes = [
            models.Index(fields=["subject_id"]),
            models.Index(fields=["client", "screen_status"]),
            models.Index(fields=["case"]),
            models.Index(fields=["eligible_status"]),
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
    """A client's answer to a question within a screening."""

    answer_id = models.UUIDField(primary_key=True, editable=False)
    screening = models.ForeignKey(
        Screening, on_delete=models.CASCADE, related_name="answers"
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
            models.Index(fields=["question"]),
        ]

    def __str__(self):
        return f"Answer {self.answer_id}"


class IdentifiedSocialNeed(models.Model):
    """A social need identified by a screening."""

    identified_social_need_id = models.UUIDField(primary_key=True, editable=False)
    screening = models.ForeignKey(
        Screening, on_delete=models.CASCADE, related_name="identified_social_needs"
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
