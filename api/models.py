import secrets
import uuid
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
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
    SCREENED = "screened", "Screening"  # >=1 completed Met Council screening
    ASSESSMENT = "assessment", "Assessment"  # completed assessment, eligible
    NAVIGATION = "navigation", "Care Management"  # >=1 Met Council case (stored value stays "navigation")
    # Screening identified a social need under an ACTIVE program we serve (see
    # api.services.lifecycle._is_eligible). Ranks above Navigation.
    ELIGIBLE = "eligible", "Eligible"
    # --- Enrollment-driven (mirror EnrollmentVerification.stage) ---
    PENDING_VERIFICATION = "pending_verification", "Pending Verification"
    VERIFIED = "verified", "Verified"
    KITCHEN_ASSIGNMENT = "kitchen_assignment", "Kitchen Assignment"  # approved auth, awaiting manual kitchen assignment
    ACTIVE = "active", "Active"  # receiving deliveries
    COMPLETED = "completed", "Completed"  # after last delivery
    # "Main stage" grouping value: any member holding a live enrollment
    # (pending_verification..completed / on_hold) rolls up to Enrolled for the
    # member-profile funnel; the per-program detail lives on ProgramStatus. Not
    # produced by the stored funnel derivation -- used by the display grouping
    # (api.services.lifecycle.main_stage) only.
    ENROLLED = "enrolled", "Enrolled"
    # Terminal main-stage grouping: every enrollment cancelled.
    CANCELLED = "cancelled", "Cancelled"
    # --- Terminal off-ramp ---
    NOT_ELIGIBLE = "not_eligible", "Not Eligible"  # ineligible / closed without service
    # --- Import-time eligibility off-ramps (api.services.eligibility) ---
    # Hard off-ramp: a CareCircle-UNFIXABLE eligibility failure (expired/missing
    # medical insurance, wrong Medicaid type, or an out-of-range primary/delivery
    # address). The member's Unite Us case must be closed by an agent. Set only by
    # reconcile_client_eligibility and kept sticky in derive_client_stage until the
    # underlying data recovers on a later import.
    INELIGIBLE = "ineligible", "Ineligible"
    # Was enrolled but now has ZERO OPEN internal-service cases (set by the cases
    # import). Distinct from INACTIVE ("never pursued / funnel start").
    SERVICE_INACTIVE = "service_inactive", "Inactive"


class ProgramStatus(models.TextChoices):
    """Display-only per-program (EnrollmentVerification) status.

    NEVER stored: computed by ``api.services.lifecycle.program_status()`` from an
    enrollment's stage + its governing internal-service case authorization +
    approval window. Merges the verification stage and the case-authorization
    dimension into one linear per-program timeline for the member Programs tab.
    """

    PENDING_VERIFICATION = "pending_verification", "Pending Verification"
    VERIFIED = "verified", "Verified"
    # Nutritionist review gate -- sits between Verified and Kitchen Assignment.
    # A verified household waits here for a Nutritionist to sign off before it can
    # advance to kitchen assignment (and thus into service / POs), regardless of
    # the case authorization outcome.
    PENDING_NUTRITIONIST = "pending_nutritionist", "Pending Nutritionist"
    NUTRITIONIST_APPROVED = "nutritionist_approved", "Nutritionist Approved"
    WAITING_AUTHORIZATION = "waiting_authorization", "Waiting Authorization"
    AUTHORIZED = "authorized", "Authorized"
    DENIED = "denied", "Denied"
    KITCHEN_ASSIGNMENT = "kitchen_assignment", "Kitchen Assignment"
    ACTIVE = "active", "Active"
    ON_HOLD = "on_hold", "On Hold"
    # A delivery-coverage hold: the household's delivery/primary ZIP is outside
    # the service area, so every member is Out of Range and the program is held.
    # Distinct from a generic On Hold so the program stage matches the members'
    # Out of Range labels (the main lifecycle stage separately reads Ineligible).
    OUT_OF_RANGE = "out_of_range", "Out of Range"
    # Final: the approval window's end date passed. A re-authorization arrives on
    # a NEW case (a new program row), never on this expired one.
    AUTHORIZATION_EXPIRED = "authorization_expired", "Authorization Expired"
    # A reauthorization handoff: the current authorization window ended and the
    # household is paused waiting for the reauthorization window to begin (the
    # gap between two windows). See docs/reauthorization_extension_plan.md.
    REAUTHORIZATION = "reauthorization", "Reauthorization"
    # Final: the governing case is closed.
    CLOSED = "closed", "Closed"


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


class ClientSource(models.TextChoices):
    """Which channel first created the member in OUR system.

    Write-once: set when the row is first inserted and never rewritten, so it
    always answers "how did this member get here?". Blank on rows that predate
    the field.
    """

    EXTENSION = "extension", "Extension"
    IMPORT = "import", "Import"


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
    # Set once we've pushed this member to Hyros as "Enrolled" (Meta Ads leads
    # with an internal-service case). Guards the once-per-member push.
    hyros_enrolled_pushed_at = models.DateTimeField(null=True, blank=True)

    # --- Lifecycle funnel (maintained by api.services.lifecycle) ---
    lifecycle_stage = models.CharField(
        max_length=25, choices=ClientStage.choices,
        default=ClientStage.INACTIVE, db_index=True,
    )
    lifecycle_stage_at = models.DateTimeField(null=True, blank=True)
    # Denormalized sort key for the Members list "Created" column: the most
    # recent internal-service case date_opened (mirrors the correlated subquery
    # the list used to run per row). Maintained by reconcile + the
    # backfill_client_case_sort command; indexed so the list orders via an index
    # scan + LIMIT instead of computing + sorting that value for all ~60k clients.
    internal_case_opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Denormalized "Added to CRM" SORT key: the MOST RECENT internal-service case
    # added_to_system_at (the A: date -- when we first saved a case for this
    # member into our DB). Maintained by reconcile + backfill_case_added_at;
    # indexed so the Members "Created" column can sort by it via an index scan.
    internal_case_added_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Denormalized "Case Created" FILTER key: the GOVERNING internal-service
    # case's date_opened (favorability/deferral aware, via
    # governing_service_case_for_display) -- so the Members list's Created filter
    # matches the Data page (which keys off the same governing case), instead of
    # matching ANY internal-service case. Maintained by reconcile +
    # backfill_client_case_sort. Indexed for a fast range filter.
    governing_internal_case_opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Denormalized "Verification Requested / Completed" FILTER keys, taken from the
    # member's GOVERNING enrollment (active_enrollment) -- so the Members /
    # Verification page's requested/completed date filters match the Data page
    # (which reports the same governing enrollment) instead of matching ANY
    # own/household enrollment. requested = enrollment ``requested_at or opened_at``;
    # completed = enrollment ``verified_at``. Maintained by refresh_internal_case_sort
    # + backfill_client_case_sort. Indexed for fast range filters.
    governing_verification_requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    governing_verification_completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Denormalized GOVERNING internal-service case attributes, so the Members list's
    # Internal-Service (open/closed), Closed-date, and Authorized-date filters key
    # off the SAME governing case the Data page reports (not ANY internal-service
    # case). Blank/NULL when there is no governing internal-service case.
    # Maintained by refresh_internal_case_sort + backfill_client_case_sort.
    # null=True is a SAFETY NET: a save(update_fields=[...]) on a partially
    # hydrated instance can leave this unset (None), and simple-history copies the
    # whole instance into the history row -- a NOT-NULL column there aborted CSV
    # imports. Treated as "" everywhere (refresh/backfill write ""; filters below
    # coalesce null to empty), so a stray null is harmless, never a crash.
    governing_internal_case_status = models.CharField(max_length=32, blank=True, null=True, default="", db_index=True)
    governing_internal_case_closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    governing_internal_case_authorized_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Denormalized GOVERNING enrollment's kitchen, so the Members list's Kitchen
    # filter keys off the same governing enrollment the Data page reports (not ANY
    # own/household enrollment's kitchen). NULL when no governing case/kitchen.
    governing_kitchen = models.ForeignKey(
        "Kitchen", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", db_index=True,
    )
    # Denormalized GOVERNING internal-service case's program TYPE
    # (household / individual, from its household_type), so the Members list's
    # Program (Household/Individual) filter keys off the governing case -- like
    # the Data page -- instead of matching a household program name on ANY
    # (incl. closed/disregarded) enrollment. "" when no governing case.
    # null=True as a safety net (see governing_internal_case_status above).
    governing_program_type = models.CharField(max_length=16, blank=True, null=True, default="", db_index=True)
    # Why the member is on the hard INELIGIBLE off-ramp: the human-readable gate
    # reasons (expired/missing Medicaid, wrong Medicaid type, out-of-range
    # ZIP/state, or a Kitchen-Assignment closure/denial). Written wherever the
    # INELIGIBLE stage is set (ext save + CSV import via reconcile_client_eligibility,
    # and the Kitchen-Assignment off-ramp) and cleared on eligibility recovery.
    ineligible_reasons = models.JSONField(default=list, blank=True)
    # Set when an agent DISREGARDS this member's pending verification. Suppresses
    # the "run verification" button until a NEW request arrives from the ext (a
    # governing pending enrollment), so a dismissed request can't be re-run
    # straight from the CRM. Never blocks a live request.
    verification_disregarded_at = models.DateTimeField(null=True, blank=True)
    # The ``case_id`` (UUID string) of the client's CURRENT governing
    # internal-service case, as chosen by ``lifecycle.governing_case_key``. Kept
    # so the case reconcile can detect when the governing case CHANGES (old ->
    # new) and record it exactly once; also lets the frontend read the program's
    # governing case directly. Empty until the first internal-service case lands.
    governing_internal_case_id = models.CharField(max_length=64, blank=True)

    # --- Unite Us person migration ---
    # When Unite Us migrates a person to a NEW canonical id (GET /people/<old>
    # -> 301 -> new), the cases re-parent to the new id while our internal
    # service state (enrollment/household/delivery) stays on the old record. We
    # consolidate onto the NEW (surviving) client and stamp the OLD id here so we
    # never re-create the duplicate and can resolve either id to the survivor.
    migrated_from_id = models.CharField(
        max_length=64, blank=True, null=True, default="", db_index=True,
    )

    # --- Tags (colour-coded labels managed in Settings) ---
    tags = models.ManyToManyField(
        "ClientTag", related_name="clients", blank=True,
    )

    # --- CRM Sync (External - GoHighLevel) ---
    crm_contact_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    # --- Metadata ---
    # SOURCE timestamps: Unite Us's own created/updated dates for the person,
    # carried through by the extension and the CSV import. NOT when we first saved
    # the member: Unite Us migrated most of this population in at the end of 2024,
    # so ~80% of rows share a handful of days in Dec-2024/Jan-2025.
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    # OUR date: when this member was first inserted into the CRM, stamped on the
    # first insert (see ``save``) and never rewritten. This is what the Data
    # page's "Member Created" range filters, because unlike ``created_at`` it
    # reflects when the member actually reached us.
    client_added_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Which channel first created the member (extension vs import). Write-once,
    # enforced in ClientSerializer._upsert. ``db_default`` so an insert that omits
    # the column (a not-yet-restarted process running older code mid-deploy) gets
    # "" instead of a NOT-NULL violation on api_client / api_historicalclient.
    source = models.CharField(
        max_length=20, choices=ClientSource.choices, blank=True, db_index=True,
        db_default="",
    )

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
    # "New client needs verification attention" flag. Set True when the client's
    # first internal-service Case is created (via the ext OR the CSV data import),
    # cleared to False once a verification completes (enrollment reaches VERIFIED).
    # Drives the Verification > "Need Attention" list and the ext screening
    # warnings. See api.services.lifecycle.advance_enrollment (clear) and
    # api.serializers.CaseSerializer (set).
    is_new = models.BooleanField(default=False, db_index=True)
    # Manually dismissed from the Urgent Care ("Need Attention") list. Independent
    # of is_new: the list no longer keys off is_new, so dismissing must set its own
    # flag (else the member would immediately reappear). Set by the Dismiss action
    # / bulk command; excluded from the need_attention scope.
    urgent_care_dismissed = models.BooleanField(default=False, db_index=True)
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
            # Members/Data list "Lead source" filter (exact match).
            models.Index(fields=["lead_source"]),
        ]

    def save(self, *args, **kwargs):
        # ``created_at`` is nullable because it's normally populated from the
        # Unite Us source record. Members created in-app (e.g. via the extension
        # sync/CRM) carry no source timestamp, so stamp it on FIRST insert only.
        # Without this they have created_at=NULL and never match the Members
        # page created-date filter (and show a blank "Created" column).
        if self._state.adding and self.created_at is None:
            self.created_at = timezone.now()
        # OUR "added to the CRM" stamp: first insert only, so it is never moved by
        # a later sync. Kept separate from ``created_at`` (the Unite Us date).
        if self._state.adding and self.client_added_at is None:
            self.client_added_at = timezone.now()
        super().save(*args, **kwargs)

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
            # Expiring-insurance filter/warnings (expired_at date range).
            models.Index(fields=["expired_at"]),
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
            # Expiring social-care-coverage filter/warnings (expired_at range).
            models.Index(fields=["expired_at"]),
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
    # An OPEN case whose authorization has never been requested (blank auth on
    # the export). In lifecycle logic it is treated exactly like a DENIAL (an
    # open case that confers no service: never governs over a real approved/
    # pending case, and drives the same full-stop when it is the top case) -- see
    # lifecycle._DENIED_EQUIVALENT_STATUSES. Only the DISPLAY label differs.
    NEVER_REQUESTED = "never_requested", "Never Requested"


class CaseType(models.TextChoices):
    """Classification of a case. Auto-derived on save by matching the case's
    program_name against the ActiveProgram table (program_name -> case_category)
    and mapping that category here; falls back to the service_type heuristic
    (Social Service Case Management => Internal Service, else => Navigation) when
    the program is not found in ActiveProgram."""

    # Stored value stays "navigation" (kept for backward-compatibility with
    # existing rows + the frontend filter id); the DISPLAY label is "Care
    # Management". Cases are classified here from the ActiveProgram
    # "Care Management" (formerly "Navigation") case_category.
    NAVIGATION = "navigation", "Care Management"
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
    # Opt-in flag: categories are inactive by default; an admin activates the
    # ones this org actually serves (mirrors Program.active). Managed from
    # Settings > Program Categories.
    is_active = models.BooleanField(default=False)
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


class ProgramParentKind(models.TextChoices):
    """The parent program a service belongs to -- what it ultimately delivers.

    SEPARATE FROM ProductTypeKind, which this field originally reused. That was
    right while the only answers were meals and boxes, but ProductTypeKind drives
    FOOD DELIVERY: reports, the logistics dashboard and order handling all branch
    on ``== ProductTypeKind.BOXES``. Adding "Home Remediation" there would push a
    housing concept into code that packs and delivers food.

    The meals and boxes VALUES are deliberately identical to ProductTypeKind's, so
    existing rows needed no data migration and anything comparing the two strings
    still agrees.
    """

    MEALS = "meals", "Meals"
    BOXES = "boxes", "Boxes"
    HOME_REMEDIATION = "home_remediation", "Home Remediation"


class DeliveryCadence(models.TextChoices):
    """How often a product type is delivered each week."""

    MON_THU = "mon_thu", "Mon/Thu"
    TUE_FRI = "tue_fri", "Tue/Fri"
    ONCE_A_WEEK = "once_a_week", "Once a Week"


# Canonical weekday codes a cadence can deliver on (matches the codes used by
# the scheduling core in api.services.delivery).
CADENCE_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# Weekly meal target per member (3 meals/day x 7 days). The per-delivery
# amounts an agent distributes across a cadence's delivery days must sum to this.
CADENCE_MEALS_PER_WEEK = 21


def default_cadence_product_quantities():
    """Default per-product delivery quantities for a new cadence.

    Meals: a fixed weekly target (``per_week``, default 21 = 3/day x 7) that the
    agent distributes across the cadence's delivery days (``per_delivery``, keyed
    by weekday code). The per-day amounts must sum to ``per_week`` but can be set
    unevenly to match real routes (e.g. Mon 9 / Thu 12). ``per_delivery`` starts
    empty and is filled once delivery days are chosen.

    Boxes: always 1 box per day (``per_day``); a delivery covering N days carries
    N boxes, so a week always totals 7.
    """
    return {
        ProductTypeKind.MEALS: {"per_week": CADENCE_MEALS_PER_WEEK, "per_delivery": {}},
        ProductTypeKind.BOXES: {"per_day": 1},
    }


class Cadence(models.Model):
    """A configurable delivery cadence (how often + on which weekdays a product
    is delivered each week), managed from Settings > Delivery Cadences.

    Seeded from the legacy :class:`DeliveryCadence` enum so the existing set
    (Mon/Thu, Tue/Fri, Once a Week) is available out of the box; new cadences
    can be added here. ``code`` is a stable slug that matches the value stored
    on ``ProductType.delivery_days_cadence`` / ``MemberDeliverySchedule`` so the
    two stay in sync. ``weekdays`` holds the delivery weekday codes (empty for a
    once-a-week cadence, where the agent picks the single day).

    NOTE: This table is currently configuration only -- the scheduling core
    still reads the legacy enum/weekday map. Wiring the scheduler to consult
    this table is a follow-up.
    """

    cadence_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=80)
    # Delivery weekday codes (subset of CADENCE_WEEKDAY_CODES). Empty means the
    # weekday is chosen per-member at assignment time (once-a-week style).
    weekdays = models.JSONField(default=list, blank=True)
    # PO / cutoff weekday per delivery day: maps a delivery weekday code to the
    # weekday its purchase order is cut on, e.g. {"mon": "thu", "thu": "mon"} for
    # a Mon/Thu meal cadence or {"tue": "fri"} for a Tuesday box cadence. The PO
    # date is the most recent occurrence of that weekday strictly before the
    # delivery. Empty falls back to the legacy hardcoded map in purchase_orders.
    po_weekdays = models.JSONField(default=dict, blank=True)
    # Per-product delivery quantities for this cadence, keyed by ProductTypeKind
    # ("meals"/"boxes"). Meals store a weekly target (``per_week``, default 21)
    # and the agent-set distribution across delivery days (``per_delivery``,
    # keyed by weekday code, summing to ``per_week``); boxes store a per-DAY rate
    # (``per_day``, always 1 -> a delivery covering N days carries N boxes).
    # Supersedes the per-(type, cadence) quantities previously read from
    # ProductType, so each cadence can define its own quantities per product.
    product_quantities = models.JSONField(
        default=default_cadence_product_quantities, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return self.label or self.code

    def meals_per_week_for(self):
        """The weekly meal target configured for this cadence (0 when unset)."""
        q = (self.product_quantities or {}).get(ProductTypeKind.MEALS) or {}
        try:
            return int(q.get("per_week") or 0)
        except (TypeError, ValueError):
            return 0

    def meals_per_delivery_for(self):
        """The agent-set per-delivery meal amounts, keyed by delivery weekday
        code (e.g. ``{"mon": 9, "thu": 12}``). Values that can't be coerced to
        ints are skipped."""
        q = (self.product_quantities or {}).get(ProductTypeKind.MEALS) or {}
        pd = q.get("per_delivery") or {}
        out = {}
        if isinstance(pd, dict):
            for wd, qty in pd.items():
                try:
                    out[wd] = int(qty or 0)
                except (TypeError, ValueError):
                    continue
        return out

    def boxes_per_day_for(self):
        """The per-DAY box rate configured for this cadence (defaults to 1). A
        delivery covering N days carries N boxes."""
        q = (self.product_quantities or {}).get(ProductTypeKind.BOXES) or {}
        try:
            return int(q.get("per_day") or 0)
        except (TypeError, ValueError):
            return 0


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
    # Programs come from Unite Us but are opt-in: inactive by default, an admin
    # activates the ones this org actually serves (Settings > Programs).
    active = models.BooleanField(default=False)

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
    # The Unite Us case CREATED timestamp (with time), taken straight from the
    # source ``created_at`` -- NO fallback to the agent-entered opened date. This
    # is the authoritative tie-breaker for governing-case selection: when a
    # member holds several open internal-service cases created the same day, the
    # most recent ``case_created_at`` (date + time) is the governing candidate.
    # ``date_opened`` is kept for display / the "Date Opened" filter (and can be
    # agent-edited), so it is NOT reliable for this ordering.
    case_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(null=True, blank=True)  # source last update
    # When this case row was FIRST saved into OUR system -- distinct from the
    # Unite Us source dates above (date_opened / case_created_at), which can
    # predate the import that actually added it. Stamped on first insert (see
    # save); backfilled for existing rows from the earliest HistoricalCase
    # history_date. Powers the Members "Created" column A: row + the "how many
    # cases did we ADD on date X" reporting (independent of the UU date's import lag).
    added_to_system_at = models.DateTimeField(null=True, blank=True, db_index=True)

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
    # Broad Unite Us service category -- the service node's PARENT in the Unite Us
    # taxonomy (e.g. "Food Assistance", code UU-FOOD). Source: the CSV export's
    # `service_type` column, and on the live API the case's `service` relationship
    # resolved to its parent service `name`. NOTE: `service_type` above actually
    # holds the SPECIFIC service (CSV `service_subtype`, e.g. "Medically Tailored
    # Meals"); this field is the broader grouping above it.
    #
    # NULLABLE: cases created before this column existed (migration 0150) carry a
    # NULL here, and the CSV/API export leaves it blank for cases with no broad
    # category. A NOT-NULL column rejected the historical-row copy on every
    # re-save of such a case (django-simple-history), rolling back the whole
    # import row -- so a re-import silently failed to update auth status. Allowing
    # NULL lets those rows save; the value is populated whenever the source
    # carries a category.
    service_category = models.CharField(max_length=255, blank=True, null=True)

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
    # True when this case is a REAUTHORIZATION / extension of an existing service
    # -- derived on upsert from a matching ActiveProgram row with ``to_extend``
    # set (see api.serializers.derive_is_extension). Drives the scheduled-
    # extension governing-case handling (see docs/reauthorization_extension_plan).
    # ``db_default`` so an insert that omits the column (e.g. a not-yet-restarted
    # process running older code mid-deploy) still gets False instead of a
    # NOT-NULL violation on api_case / api_historicalcase.
    is_extension = models.BooleanField(default=False, db_default=False)

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
    # --- Service Authorization decision detail (Unite Us
    # /v1/service_authorizations attributes) ---
    # The adjudicator's decision note (e.g. "This authorization was
    # automatically accepted." or a denial rationale) -- this is the UI's
    # "Decision Note". Maps from the auth's ``adjudicator_note``.
    service_authorization_decision_note = models.TextField(blank=True)
    # Comment left while the authorization sat in review (``in_review_note``).
    service_authorization_in_review_note = models.TextField(blank=True)
    # Comment attached to an "update requested" action (``update_request_note``).
    service_authorization_update_request_note = models.TextField(blank=True)
    # Payer-side authorization number, when the payer supplies one
    # (``payer_authorization_number``); usually null for auto-approved auths.
    payer_authorization_number = models.CharField(max_length=120, blank=True)
    # When the authorization was submitted for decision (``submitted_at``).
    service_authorization_submitted_at = models.DateTimeField(null=True, blank=True)
    # True when Unite Us auto-approved the authorization (``auto_approved``);
    # null when the source didn't report it.
    service_authorization_auto_approved = models.BooleanField(null=True, blank=True)
    # True when the authorization was flagged urgent (``urgent``); null when the
    # source didn't report it.
    service_authorization_urgent = models.BooleanField(null=True, blank=True)
    # Coded denial reason on a DENIED authorization (the auth's
    # ``service_authorization_denial_reason`` relationship). We store the id plus
    # the resolved human-readable name; both blank on non-denied auths. This is
    # the structured "why" that complements the free-text decision note.
    service_authorization_denial_reason_id = models.UUIDField(null=True, blank=True)
    service_authorization_denial_reason = models.CharField(max_length=255, blank=True)
    # Authorized unit COUNT from the authorization (``approved_unit_amount``,
    # falling back to ``requested_unit_amount``). Distinct from ``authorized_unit``
    # (the unit TYPE, e.g. "meals") and ``authorized_amount`` (dollars). Free text
    # to mirror the per-service ContractedService.authorized_units.
    authorized_units = models.CharField(max_length=80, blank=True)

    # --- Social Care Coverage (as shown on the case) ---
    social_care_coverage_plan = models.CharField(max_length=255, blank=True)
    social_care_coverage_status = models.CharField(max_length=80, blank=True)

    # --- CRM Sync Tracking (External - GoHighLevel Opportunity) ---
    crm_opportunity_id = models.CharField(max_length=64, blank=True, db_index=True)
    crm_sync_hash = models.CharField(max_length=64, blank=True)
    crm_synced_at = models.DateTimeField(null=True, blank=True)

    history = tracked_history()

    def save(self, *args, **kwargs):
        # Stamp the date we FIRST saved this case into our DB (first insert only),
        # so it survives later source-driven updates. Historical rows are
        # backfilled from the earliest HistoricalCase.history_date.
        if self._state.adding and self.added_to_system_at is None:
            self.added_to_system_at = timezone.now()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-date_opened"]
        indexes = [
            models.Index(fields=["client", "case_status"]),
            models.Index(fields=["provider"]),
            models.Index(fields=["originating_provider"]),
            models.Index(fields=["program"]),
            models.Index(fields=["case_status"]),
            models.Index(fields=["created_by_id"]),
            # Powers the Members-list ORDER BY correlated subquery (per client:
            # latest internal-service case by date_opened). Without it that
            # subquery scans all a client's cases + filters case_type + sorts,
            # ~60k times per page -> the query blew up to minutes after a large
            # purge left api_case bloated. Composite = single index lookup.
            models.Index(
                fields=["client", "case_type", "-date_opened"],
                name="api_case_client_type_dopen_idx",
            ),
            # Members/Data list default filter: internal-service cases opened in a
            # date window (created_from/created_to -> case_type + date_opened,
            # across ALL cases before the client join). Index the global predicate.
            models.Index(
                fields=["case_type", "date_opened"],
                name="api_case_type_dopen_idx",
            ),
            # "Added to CRM" filter (added_from/added_to -> case_type +
            # added_to_system_at, across ALL cases before the client join).
            models.Index(
                fields=["case_type", "added_to_system_at"],
                name="api_case_type_added_idx",
            ),
            # Closed-date filter (closed_from/closed_to -> case_closed_at).
            models.Index(
                fields=["case_type", "case_closed_at"],
                name="api_case_type_closed_idx",
            ),
        ]

    def __str__(self):
        return f"Case {self.case_id} ({self.get_case_status_display()})"

    def effective_authorization_window(self):
        """``(start, end)`` datetimes of the case's authorization window.

        Prefers the APPROVAL window (``service_authorization_approval_starts_at`` /
        ``_ends_at``). When the case is APPROVED (or NOT_REQUIRED) but the approval
        window was not exported -- some Unite Us exports carry only the REQUEST
        window on an already-approved authorization -- fall back to the request
        window so an approved case still yields a usable service window instead of
        stranding the household out of service. Each endpoint falls back
        independently. Returns ``(None, None)`` when neither is set.
        """
        start = self.service_authorization_approval_starts_at
        end = self.service_authorization_approval_ends_at
        if (start is None or end is None) and self.service_authorization_status in (
            ServiceAuthorizationStatus.APPROVED,
            ServiceAuthorizationStatus.NOT_REQUIRED,
        ):
            start = start or self.service_authorization_request_starts_at
            end = end or self.service_authorization_request_ends_at
        return start, end


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

    # --- Accountability ---
    # The Unite Us facilitator who performed the screening. NB: the screening
    # export's ``facilitator_id`` maps to ``UniteUsAgent.employee_id`` (NOT
    # ``user_id``, which is what cases/assessments use) -- see csv_import. Only
    # populated by the CSV import (the extension push carries no facilitator id).
    facilitator_id = models.UUIDField(null=True, blank=True, db_index=True)

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

    # --- Accountability ---
    # The Unite Us user who submitted the assessment. The assessments export's
    # ``submission_created_by_id`` maps to ``UniteUsAgent.user_id`` -- the SAME
    # key the cases export's ``case_created_by_id`` uses (unlike screenings,
    # whose ``facilitator_id`` maps to ``employee_id``). Only populated by the
    # CSV import (the extension push carries no creator id).
    created_by_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_by_name = models.CharField(max_length=255, blank=True)
    # The screenings-ingestion API attributes an assessment to a ``facilitator_id``
    # which is an ``employee_id`` (→ UniteUsAgent.employee_id) -- a DIFFERENT id
    # space than the CSV export's ``submission_created_by_id`` (a ``user_id`` →
    # created_by_id above). Stored separately so the API-sourced facilitator never
    # collides with the CSV-sourced creator; the accountability dashboard unifies
    # both keys through UniteUsAgent. Populated by the API mapper + the extension.
    facilitator_id = models.UUIDField(null=True, blank=True, db_index=True)

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
    # A pending verification REQUEST an agent dismissed from the pop-up (the
    # member shouldn't be verified -- e.g. requested in error). Non-terminal:
    # the row is KEPT for history (dietary profiles, delivery address + case
    # link preserved) but is excluded from lifecycle governance and the
    # Verification list, so the member reverts to their pre-verification funnel
    # stage. A fresh request (from the ext) creates a NEW enrollment. See
    # api.services.lifecycle + api.portal.views_members.
    DISREGARDED = "disregarded", "Disregarded"
    # A verified household's REAUTHORIZATION / extension enrollment, parked and
    # NON-SERVING until its authorization window becomes effective. It carries the
    # full (already-verified) roster + dietary data so activation is a clean
    # promotion to Service Active. Excluded from every serving surface (POs,
    # Distribution matrix, calendar, verification queue) until it activates. See
    # docs/reauthorization_extension_plan.md.
    SCHEDULED_EXTENSION = "scheduled_extension", "Reauthorization - Waiting"


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

    ``PENDING`` is the INITIAL state every member (incl. the primary) sits at
    from creation through verification and Nutritionist review -- i.e. before a
    kitchen is assigned. A member only becomes ``ACTIVE`` once the kitchen-
    assignment meal rule can fulfill them (else ``OUT_OF_ORBIT``). Unlike the
    terminal ``INACTIVE``, the automatic meal rule DOES evaluate + promote a
    PENDING member; but like every non-ACTIVE status, a PENDING member is NEVER
    placed on a delivery schedule or Purchase Order.
    """

    # Initial / pre-kitchen state: created here and held through verification +
    # Nutritionist review until kitchen assignment activates them.
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    OUT_OF_ORBIT = "out_of_orbit", "Out of Orbit"
    # Set automatically when the member's DELIVERY or PRIMARY address ZIP is
    # outside the service coverage area (not in the active ServiceZipCode
    # whitelist). Like
    # OUT_OF_ORBIT, out-of-range members are excluded from all delivery schedules
    # and Purchase Orders. Unlike Out of Orbit (a dietary/kitchen fulfillment
    # block), Out of Range also opens a Case Closure ticket and holds the whole
    # household -- see api.portal.views_members._enforce_delivery_coverage.
    OUT_OF_RANGE = "out_of_range", "Out of Range"
    # Agent-initiated manual pause (requires a reason note). Like OUT_OF_ORBIT,
    # paused members are excluded from all delivery schedules and Purchase
    # Orders until an agent unpauses them (which re-runs the meal rule).
    PAUSED = "paused", "Paused"
    # Terminal off-ramp: the member was in service and no longer is. Like
    # OUT_OF_ORBIT/PAUSED, inactive members are excluded from all delivery
    # schedules and Purchase Orders. Unlike a pause this is an end state (their
    # service ended), not a temporary hold.
    INACTIVE = "inactive", "Inactive"
    # A Nutritionist reviewed the member and paused them (requires a reason note).
    # Like OUT_OF_ORBIT, a paused member is excluded from all delivery schedules
    # and Purchase Orders. Set per member from the Nutritionist review drawer;
    # independent of the rest of the household (but pausing the LAST active member
    # holds the whole household -- see MemberNutritionistDenyMemberView).
    NUTRITIONIST_PAUSED = "nutritionist_paused", "Nutritionist Paused"
    # Terminal HISTORY state: the member was split out of this household into their
    # OWN internal-service case (see api.services.household_split). Their profile
    # row is KEPT on the old household enrollment for history but is excluded from
    # all service AND from the household roster re-sync (so it is not re-added).
    REMOVED = "removed", "Removed"


# Statuses an agent may PAUSE from. ACTIVE is the original case; PENDING and
# INACTIVE were added because they had no pause action at all, leaving an agent
# no way to record that a member is on hold before service ever started (or after
# it ended). Deliberately NOT out-of-orbit / out-of-range, which have their own
# remedies (fix the menu, fix the ZIP), nor an eligibility pause, which is not
# ours to lift.
PAUSABLE_MEMBER_STATUSES = (
    MemberStatus.ACTIVE,
    MemberStatus.PENDING,
    MemberStatus.INACTIVE,
)


# Member statuses that exclude a member from every delivery schedule / order /
# Purchase Order: PENDING (pre-kitchen, not activated yet), OUT_OF_ORBIT (meal
# rule can't fulfill them), OUT_OF_RANGE (delivery/primary ZIP outside coverage),
# PAUSED (agent manually paused them), INACTIVE (service ended) and
# NUTRITIONIST_PAUSED (a Nutritionist paused them). Only ACTIVE members receive
# deliveries.
SERVICE_EXCLUDED_MEMBER_STATUSES = (
    MemberStatus.PENDING,
    MemberStatus.OUT_OF_ORBIT,
    MemberStatus.OUT_OF_RANGE,
    MemberStatus.PAUSED,
    MemberStatus.INACTIVE,
    MemberStatus.NUTRITIONIST_PAUSED,
    MemberStatus.REMOVED,
)

# Statuses that mean a member is no longer "in play" for the household -- a
# genuine pause / off-ramp (NOT the pre-kitchen PENDING, which is still active in
# the pipeline). Used to decide when the LAST real member has been paused so the
# whole household should be held.
# The statuses that count as a PAUSE for paused_at / resumed_at.
#
# ⚠ NARROWER than MEMBER_PAUSED_STATUSES, which also contains INACTIVE. Inactive is a
# terminal end state -- "their service ended" -- not a pause anybody resumes from, and
# including it would stamp paused_at on 1,353 members the Paused tab does not even
# list. Same call the tab and the backfill make.
MEMBER_PAUSE_TIMESTAMP_STATUSES = (
    MemberStatus.OUT_OF_ORBIT,
    MemberStatus.OUT_OF_RANGE,
    MemberStatus.PAUSED,
    MemberStatus.NUTRITIONIST_PAUSED,
)

MEMBER_PAUSED_STATUSES = (
    MemberStatus.OUT_OF_ORBIT,
    MemberStatus.OUT_OF_RANGE,
    MemberStatus.PAUSED,
    MemberStatus.INACTIVE,
    MemberStatus.NUTRITIONIST_PAUSED,
)

# Enrollment stages that exclude a whole household from Purchase Order / delivery
# generation: ON_HOLD (a problem was detected and the case is under review, and
# may be heading to closure -- distinct from a benign, temporary MemberStatus.PAUSED),
# KITCHEN_ASSIGNMENT (awaiting a manual kitchen + cadence assignment -- with no
# kitchen the household is not deliverable, so it must never feed a PO; e.g. a
# meals<->boxes product switch requeues the household here and its OLD calendar
# must not keep shipping until a NEW kitchen/cadence is assigned, which advances
# it to SERVICE_ACTIVE and rebuilds the calendar), plus the terminal stages
# SERVICE_COMPLETE / CLOSED / CANCELLED (service has ended -- e.g. a cancelled /
# off-boarded household must never appear on a new PO or delivery).
#
# NOTE: excluding KITCHEN_ASSIGNMENT is a no-op for a normally-onboarding
# household (it holds no delivery calendar until a kitchen is assigned, and the
# assignment flow builds the calendar THEN advances to SERVICE_ACTIVE in one
# request -- MemberAssignKitchenView). It only closes the leak where a stale /
# requeued household still carries occurrences at this stage.
SERVICE_EXCLUDED_ENROLLMENT_STAGES = (
    EnrollmentStage.ON_HOLD,
    EnrollmentStage.KITCHEN_ASSIGNMENT,
    EnrollmentStage.SERVICE_COMPLETE,
    EnrollmentStage.CLOSED,
    EnrollmentStage.CANCELLED,
    # A parked reauthorization extension never serves until it activates.
    EnrollmentStage.SCHEDULED_EXTENSION,
)


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
    # A schedule that exists for DISPLAY on a parked reauthorization
    # (SCHEDULED_EXTENSION) enrollment but is NOT yet serving: never generates
    # Purchase Order occurrences (every occurrence/PO path filters
    # status=SCHEDULED). Flipped to SCHEDULED — actually rebuilt fresh — when the
    # reauthorization activates. See docs/reauthorization_extension_plan.md.
    WAITING = "waiting", "Waiting"


class ScheduleCadence(models.TextChoices):
    ONCE = "once", "One-time"
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Bi-weekly"
    MONTHLY = "monthly", "Monthly"


class StageEntityType(models.TextChoices):
    CLIENT = "client", "Client"
    ENROLLMENT = "enrollment", "Enrollment"
    # Housing work dispatched to a vendor. Joins this log rather than getting its
    # own, so "everything that happened to this member" stays ONE query -- a
    # parallel table would need every history view, export and report to learn
    # about it.
    DISPATCH_ORDER = "dispatch_order", "Dispatch Order"


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
    # The Met Council case this enrollment is delivered under. Bound at
    # verification (the governing internal-service case picked in the wizard) and
    # kept in sync through governing-case replacements -- every enrollment should
    # reference its case; it's only ever null in the brief window before the
    # client's first case exists.
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollments",
    )
    # The case this enrollment REPLACED (its predecessor's governing case), set
    # when it's forked from a prior enrollment during a governing-case
    # replacement. Preserves the prior-case link for history/audit even after the
    # old enrollment is closed.
    previous_case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="superseded_by_enrollments",
    )
    # Set on a legacy CASELESS placeholder "previous enrollment" that has no
    # distinct prior case to attach to (misinformation from before enrollments
    # were guaranteed a case). Hidden from the Program tab's previous-enrollments
    # list and safe to purge later.
    hidden_misinformation = models.BooleanField(default=False, db_index=True)
    # The kitchen assigned to fulfill this household's deliveries. One kitchen
    # serves the whole household (members are never split across kitchens). Set
    # on the Logistics page; editable from the member profile. NULL until
    # assigned.
    kitchen = models.ForeignKey(
        "Kitchen", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollment_verifications",
    )
    # Manual meals/boxes correction for THIS household. Product kind is normally
    # derived (program -> ProductType link, then program-name keyword); when that
    # detection is wrong (e.g. a boxes case classified as meals), an agent sets
    # this on the Household tab and the resolver (product_kind_for_enrollment)
    # honors it FIRST. NULL = use the derived kind.
    product_type_override = models.ForeignKey(
        "ProductType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="override_enrollments",
    )
    # Manual Household/Individual scope correction for THIS household. The scope
    # is normally derived from the governing case's program name (see
    # ``derive_household_type``); when that is wrong an agent sets this on the
    # Household tab and readers honor it FIRST. Blank = use the derived scope.
    household_type_override = models.CharField(
        max_length=12, choices=CaseHouseholdType.choices, blank=True, default="",
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
    # Nutritionist sign-off gate. A verified household sits at Pending Nutritionist
    # until a Nutritionist approves it here (a legal sign-off: the typed signature
    # + who + when are the audit trail). Only then may an approved authorization
    # advance the enrollment to Kitchen Assignment (see
    # reconcile_enrollment_authorization). NULL == not yet approved.
    nutritionist_approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    nutritionist_approved_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nutritionist_approved_enrollments",
    )
    # The Nutritionist's typed signature captured at approval (their full name).
    nutritionist_signature = models.CharField(max_length=255, blank=True)
    # The drawn signature (PNG data URL) captured at approval, and the S3 key of
    # the generated signed Nutrition Review PDF (downloadable from the member's
    # Nutrition tab).
    nutritionist_signature_image = models.TextField(blank=True)
    nutritionist_approval_pdf_key = models.CharField(max_length=500, blank=True)
    # Short display code, e.g. "ENR-8754". Assigned on creation.
    code = models.CharField(max_length=20, blank=True, db_index=True)
    # Renewal cycle counter. Renewals reuse the SAME enrollment (re-run
    # screening/assessment/verification) rather than creating a new row; this
    # tracks which cycle the enrollment is on (1 = initial, 2 = first renewal…).
    renewal_number = models.PositiveSmallIntegerField(default=1)
    stage_at = models.DateTimeField(null=True, blank=True)
    # WHY this programme is On Hold, when it is. Set by whichever check or agent
    # placed the hold, and CLEARED on resume -- a reason left on a serving
    # enrollment reads as a current problem.
    #
    # Only meaningful while stage == ON_HOLD. Kept on the enrollment rather than
    # derived from the latest StageEvent note because the Program tab, the Members
    # page and any future hold queue all need to FILTER on it, and parsing 443
    # distinct note strings is how this was unanswerable in the first place.
    hold_reason = models.ForeignKey(
        "HoldReason", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enrollments",
    )
    # WHEN the programme was held, and when it came back. Stamped in
    # advance_enrollment, the single place a stage transition is written.
    #
    # held_at survives the resume, so "held on the 3rd, resumed on the 11th" is
    # answerable. Current cycle only -- the full history is in the StageEvents,
    # which each carry their own hold_reason.
    held_at = models.DateTimeField(null=True, blank=True, db_index=True)
    hold_resumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    # The agent who REQUESTED the verification -- i.e. submitted the E-Form that
    # created this enrollment (opened_at is the request time). Set on creation
    # from the authenticated extension agent; NULL for bulk-imported enrollments
    # where no acting agent is attributable.
    requested_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requested_enrollments",
    )
    # When the verification was last REQUESTED. Distinct from opened_at (row
    # creation): re-requesting/renewing an existing unverified enrollment stamps
    # this to now() and repoints requested_by, so the CRM's "Requested" column
    # reflects the latest request (and acting agent) without falsifying the
    # creation time. Falls back to opened_at when never explicitly stamped.
    requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    # Supersession link for the close-old / open-new enrollment replacement
    # (enrollment-case-replacement-plan.md). New enrollment points at the one it
    # replaced; old enrollment's related_name "superseded_by" yields the new one.
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="superseded_by",
    )
    # Why this enrollment was closed (e.g. "case_replaced"). Extra details such
    # as old/new product kind, old/new scope, and old/new case ids are kept in
    # close_context for audit and UI history.
    close_reason = models.CharField(max_length=32, blank=True, default="")
    close_context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["client", "stage"]),
            models.Index(fields=["household", "stage"]),
            # Verification-requested date filter (requested_from/to -> opened_at).
            models.Index(fields=["opened_at"]),
            # NOTE: kitchen + verified_by are ForeignKeys and are ALREADY indexed
            # by Django's automatic FK index -- no explicit index needed (see
            # migration that drops the earlier redundant duplicates).
        ]
        constraints = [
            # At most one LIVE verification per (navigation) case. Renewals reuse
            # the same row, so this never blocks a renewal. Terminal rows --
            # disregarded, cancelled, and CLOSED (a superseded/replaced
            # enrollment kept as read-only history) -- are excluded here, so when
            # a case governs again a fresh enrollment can reuse it without
            # colliding with its own history. NULL case is unconstrained.
            models.UniqueConstraint(
                fields=["case"],
                condition=models.Q(case__isnull=False)
                & ~models.Q(stage__in=["disregarded", "cancelled", "closed"]),
                name="uniq_enrollment_verification_per_case",
            ),
        ]

    def __str__(self):
        return f"{self.client_id} ({self.stage})"


# Medical Conditions OFFERED in the verification wizard (Step 2) and the
# nutritionist intake. Stored as labels on MemberDietaryProfile.conditions;
# "No Restriction" is the default / nothing-selected sentinel.
SELECTABLE_MEMBER_CONDITIONS = [
    "Cancer", "Congestive Heart Failure", "Crohn’s Disease", "Diabetic",
    "Gestational Diabetes", "Heart disease", "High blood pressure",
    "High cholesterol", "Hypothyroidism", "Hyperthyroidism", "IBS",
    "Kidney Disease Non-Dialysis", "Kidney Disease ON Dialysis",
    "Liver Disease", "Overweight (determined by BMI)",
    "Obesity (determined by BMI)", "Pre-Diabetes", "Postpartum", "Pregnant",
    "Ulcerative Colitis", "No Restriction",
]

# RETIRED conditions: no longer offered, but NOT removed from members who
# already carry one. Deleting a clinical value someone recorded deliberately
# would destroy history, so these stay valid stored labels -- they still render
# on the nutritionist pages and remain searchable in the Data page's free-text
# conditions filter. "Kidney Disease" was split into the Non-Dialysis / ON
# Dialysis pair above; "Cardiometabolic" was replaced by Congestive Heart
# Failure.
RETIRED_MEMBER_CONDITIONS = [
    "Cardiometabolic",
    "Kidney Disease",
]

# Every label that may legitimately appear on a stored profile.
MEMBER_CONDITIONS = SELECTABLE_MEMBER_CONDITIONS + RETIRED_MEMBER_CONDITIONS


# Medications OFFERED in the verification wizard + the nutritionist intake,
# stored as labels on MemberDietaryProfile.medications. Kept in clinical groups
# (cardiac, diabetes/weight, statins/BP, oncology, GI, thyroid) rather than
# alphabetized -- the pickers sort for display. "No Medications" is the
# nothing-selected sentinel, added by the picker alongside its "Other" option.
SELECTABLE_MEMBER_MEDICATIONS = [
    "Aspirin", "Clopidogrel", "Nitroglycerin (sublingual)", "Lisinopril",
    "Metoprolol succinate", "Furosemide", "Amiodarone", "Diltiazem", "Sotalol",
    "Insulin", "Metformin", "Empagliflozin", "Semaglutide",
    "Insulin glargine or lispro", "Orlistat", "Atorvastatin", "Rosuvastatin",
    "Simvastatin", "Amlodipine", "Losartan", "Paclitaxel", "Carboplatin",
    "Methotrexate", "Mesalamine", "Prednisone", "Azathioprine",
    "Polyethylene glycol (PEG)", "Linaclotide", "Lubiprostone", "Loperamide",
    "Dicyclomine", "Rifaximin", "Calcitriol", "Propranolol", "Spironolactone",
    "Lactulose", "Methimazole", "Propylthiouracil (PTU)", "Levothyroxine",
]

# Retired medications: same contract as RETIRED_MEMBER_CONDITIONS -- no longer
# offered, but never stripped from a member who already has one. Empty today; the
# shape exists so retiring one is a list move rather than a data migration.
RETIRED_MEMBER_MEDICATIONS = []

# Every label that may legitimately appear on a stored profile.
MEMBER_MEDICATIONS = SELECTABLE_MEMBER_MEDICATIONS + RETIRED_MEMBER_MEDICATIONS


def default_member_conditions():
    """Default value for MemberDietaryProfile.conditions (nothing selected)."""
    return ["No Restriction"]


class HoldResumePolicy(models.TextChoices):
    """What it takes to come OFF a hold. Descriptive, not behaviour.

    Recorded on the reason so the catalogue carries the decision -- an agent looking
    at a held household can see whether to wait, act, or that nothing will clear it.
    No code acts on this yet; the auto-resume rules are a separate piece.
    """

    NONE = "none", "No resume — an agent must close or re-open the case"
    AUTO = "auto", "Resumes automatically when the situation changes"
    MANUAL = "manual", "An agent resumes when ready"


class HoldReason(models.Model):
    """WHY a household's programme is On Hold -- a catalogue, not free text.

    The same problem PauseReason solved for members, one level up: 7,379 holds across
    443 distinct notes, of which 1,961 are agent free text with 1,556 distinct
    reasons. "How many households are held pending case closure?" could not be asked.

    ``is_system`` marks the reasons a CHECK sets -- a denied case, a closed case, an
    expired insurance, an out-of-coverage ZIP. They are hidden from the agent's
    picker: choosing "Governing Case Denied" by hand would assert something the case
    data has not said.
    """

    hold_reason_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False,
    )
    code = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=80)
    # What clears it. See HoldResumePolicy -- descriptive only.
    resume_policy = models.CharField(
        max_length=10, choices=HoldResumePolicy.choices,
        default=HoldResumePolicy.NONE,
    )
    # What clears it, in words an agent reads. The policy says "auto"; this says
    # "when a new governing case is saved", which is the useful half.
    resume_detail = models.CharField(max_length=200, blank=True)
    is_system = models.BooleanField(default=False)
    # Retired reasons stop being offered but still render on the holds citing them.
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self):
        return self.label


class PauseReason(models.Model):
    """WHY a member is not being served -- a catalogue, not free text.

    Replaces a free-text box that produced 200 DISTINCT STRINGS across 1,341
    pauses, of which one bulk campaign ("9/1 HH Close") was 937 and one entry was a
    pasted UUID. Nothing could be counted, filtered or acted on.

    A TABLE rather than TextChoices because agents add and retire reasons without a
    deploy -- the same call Settings > Tags makes. ``code`` is the stable key the
    code refers to; ``label`` is what an agent sees and may be renamed freely.

    ``is_system`` marks the reasons only the SYSTEM sets (Out of Orbit, Out of
    Range, Nutritionist Paused, Case Type Switch, Insurance expired or invalid).
    They are hidden from the agent's picker: an agent choosing "Out of Range" by
    hand would assert something the ZIP check has not found, and the remedy for
    each is specific.
    """

    pause_reason_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False,
    )
    code = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=80)
    # Reasons the SYSTEM owns -- not offered in the agent picker.
    is_system = models.BooleanField(default=False)
    # Hidden from selection but kept on historical rows, the same treatment
    # ActiveProgram.is_active gives a retired programme: a pause that cites a
    # retired reason must still render rather than go blank.
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self):
        return self.label


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
    # Medical Conditions captured during verification (wizard Step 2). A
    # multi-select stored as a list of labels (see MEMBER_CONDITIONS); empty /
    # unselected means "No Restriction". Distinct from ``dietary_restrictions``
    # (which drives menu/meal rules) -- this is clinical context.
    conditions = models.JSONField(default=default_member_conditions, blank=True)
    # Conditional follow-ups tied to specific conditions above.
    weeks_gestation = models.PositiveSmallIntegerField(  # when "Pregnant"
        null=True, blank=True
    )
    months_postpartum = models.PositiveSmallIntegerField(  # when "Postpartum"
        null=True, blank=True
    )
    # Clinical intake captured at verification, shown to the Nutritionist.
    # Medications is a multi-select (list of labels, see MEDICATION_OPTIONS);
    # weight/height are free text so agents can add units (e.g. "180 lb").
    medications = models.JSONField(default=list, blank=True)
    weight = models.CharField(max_length=50, blank=True)
    height = models.CharField(max_length=50, blank=True)
    # Meal plan the Nutritionist selects for this member (free text -- the name of
    # a MealPlan catalog entry; see Settings > Meal Plans). "Other" lets the
    # Nutritionist type a custom plan in ``meal_plan_other``.
    meal_plan = models.CharField(max_length=150, blank=True)
    meal_plan_other = models.TextField(blank=True)
    # Verification question: is the member on any medical diet? When yes, the
    # free-text details are captured for the Nutritionist.
    on_medical_diet = models.BooleanField(default=False)
    medical_diet_details = models.TextField(blank=True)
    # The Nutritionist's assessment notes for this member (set from the review
    # drawer; shown on the member Nutritionist tab + the signed PDF).
    assessment_notes = models.TextField(blank=True)
    # S3 key of this member's OWN signed Nutrition Review PDF (one per member,
    # generated at approval with the shared signature).
    nutritionist_pdf_key = models.CharField(max_length=500, blank=True)
    meal_category = models.CharField(
        max_length=20, choices=MenuCategory.choices, blank=True
    )
    # Stores the admin-managed catalog ``MenuType`` (model) NAME, e.g.
    # "Standard", "Dairy Free", "Vegetarian", "Kosher", "Halal". (Historically
    # this held a short code from the ``MenuType`` TextChoices; a data migration
    # converts those to names.)
    menu_type = models.CharField(max_length=120, blank=True)
    # Per-member service status. Defaults to PENDING (pre-kitchen): a member is
    # only promoted to ACTIVE (or OUT_OF_ORBIT) by the meal rule at kitchen
    # assignment. See api.services.meal_rules.
    status = models.CharField(
        max_length=20, choices=MemberStatus.choices,
        default=MemberStatus.PENDING, db_index=True,
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
    # When ``status`` last changed (e.g. Active -> Out of Orbit / Paused).
    # Distinct from ``updated_at`` (any edit); stamped in ``save()`` only when the
    # status value actually flips, so the UI can show "Paused/Out of Orbit since".
    status_changed_at = models.DateTimeField(null=True, blank=True)
    # WHEN the member was paused, and when they came back. Stamped by save() off the
    # status transition, so every pause path gets them without eleven call sites
    # having to remember -- which is exactly how pause_reason was silently dropped
    # in two places earlier.
    #
    # Both are kept after the fact: paused_at survives the resume, so "paused on the
    # 3rd, resumed on the 11th" is answerable. They are the CURRENT cycle only -- the
    # full history lives in the notes and the status timeline.
    paused_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Set True when a governing-case Household->Individual switch auto-pauses this
    # (additional) member: the member is PINNED so an agent cannot un-pause them
    # from the Program tab. Cleared ONLY when Customer Service dismisses the
    # matching CaseMismatchFlag (never auto-cleared on a switch back to
    # household). See api.services.lifecycle governing-case switch handling.
    pause_locked = models.BooleanField(default=False)
    # WHY this member is not being served. Set for every pause, whoever caused it:
    # an agent picking from the Program tab, the import eligibility gate, a
    # case-type switch, the Nutritionist, or an Out of Orbit / Out of Range check.
    #
    # NOT redundant with `status`. Out of Orbit and Out of Range are statuses AND
    # reasons, but PAUSED is 1,513 members with a dozen different causes -- and one
    # field that always answers "why is this member not being served?" is worth more
    # than a status an agent has to interpret. SET_NULL so retiring a reason cannot
    # delete a member's profile.
    pause_reason = models.ForeignKey(
        "PauseReason", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="member_profiles",
    )
    # The status this member held BEFORE an agent paused them, so an unpause puts
    # them back where they were. Without it, unpause re-runs the meal rule and
    # lands everyone on ACTIVE -- which would make pause+unpause a way to
    # activate a PENDING member without kitchen assignment or the nutritionist
    # sign-off, and to revive a terminal INACTIVE without the explicit "Return
    # this member to service" flow (which re-checks the kitchen and requires a
    # menu type). Blank for a member paused while ACTIVE -- they keep the old
    # behaviour of being re-evaluated by the meal rule on unpause.
    pause_prior_status = models.CharField(
        max_length=32, blank=True, choices=MemberStatus.choices,
    )
    # The member's mobile number, collected during verification. Stored on the
    # enrollment (not just the client) so it is part of the verification record
    # and CARRIES ACROSS a governing-case replacement -- like the delivery
    # address. Required for the PRIMARY member; optional for dependents. Also
    # mirrored to HouseholdMember.mobile_app_username (Benefully app login).
    mobile_number = models.CharField(max_length=32, blank=True)
    # Set True when the import-time eligibility gate paused THIS member (expired/
    # missing insurance, wrong Medicaid type, out-of-range address, or missing
    # social-care coverage) -- as opposed to a manual agent pause or a scope-
    # switch pause. Lets the recovery pass un-pause ONLY eligibility-driven pauses
    # when the member's data later passes the gates. See api.services.eligibility.
    eligibility_paused = models.BooleanField(default=False)

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

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # Remember the persisted status so save() can detect a change without an
        # extra query.
        instance._loaded_status = instance.status
        return instance

    def save(self, *args, **kwargs):
        loaded = getattr(self, "_loaded_status", None)
        changed = self._state.adding or loaded is None or loaded != self.status
        if changed:
            now = timezone.now()
            self.status_changed_at = now
            extra = ["status_changed_at"]
            # INTO a pause, or OUT of one. Read from the loaded status, so a save
            # that does not move the member in or out of a pause leaves both alone.
            was_paused = loaded in MEMBER_PAUSE_TIMESTAMP_STATUSES
            now_paused = self.status in MEMBER_PAUSE_TIMESTAMP_STATUSES
            if now_paused and not was_paused:
                self.paused_at = now
                # Cleared, so a member paused again does not look resumed.
                self.resumed_at = None
                extra += ["paused_at", "resumed_at"]
            elif was_paused and not now_paused:
                self.resumed_at = now
                extra += ["resumed_at"]
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                missing = [f for f in extra if f not in update_fields]
                if missing:
                    kwargs["update_fields"] = list(update_fields) + missing
        super().save(*args, **kwargs)
        self._loaded_status = self.status

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
    # Housing dispatch. A lazy reference because DispatchOrder is defined further
    # down the module.
    dispatch_order = models.ForeignKey(
        "DispatchOrder", on_delete=models.CASCADE, null=True, blank=True,
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
    # The hold CATEGORY for a "to On Hold" event. Stamped here as well as on the
    # enrollment because the enrollment only carries the CURRENT reason -- a
    # household held in July, resumed, and held again in September would otherwise
    # lose why July happened, and 443 distinct note strings is what made that
    # unanswerable. NULL on every other kind of transition.
    hold_reason = models.ForeignKey(
        "HoldReason", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stage_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-entered_at"]
        indexes = [
            models.Index(fields=["entity_type", "to_stage"]),
            models.Index(fields=["client", "entered_at"]),
            models.Index(fields=["enrollment", "entered_at"]),
            models.Index(fields=["dispatch_order", "entered_at"]),
        ]

    def __str__(self):
        ref = self.enrollment_id or self.client_id
        return f"{self.entity_type} {ref}: {self.from_stage or '-'} -> {self.to_stage}"


class Agent(models.Model):
    """Agent/Employee who uses the extension system. Validated by agent code."""

    AGENT_GROUPS = [
        ("Screeners", "Screeners"),
        ("Verifiers", "Verifiers"),
        ("Nutritionist", "Nutritionist"),
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
    # Williamsburg agent: every client this agent saves is forced to
    # lead_source="Williamsburg" (which derives Client.is_williamsburg and
    # fast-tracks verification). Set from Settings > Williamsburg Setup and
    # enforced in api.views.ClientViewSet on save.
    is_williamsburg_agent = models.BooleanField(default=False, db_index=True)
    # Saved handwritten signature (a PNG data URL) the agent can reuse instead of
    # drawing it every time -- e.g. a Nutritionist applying it to the Nutrition
    # Case Review sign-off with one click. Blank until they save one.
    signature_image = models.TextField(blank=True)
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


class ServiceZipCode(models.Model):
    """A ZIP code in the PHS service-area WHITELIST (Manhattan / Brooklyn /
    Queens).

    Editable from Settings (add / remove / activate-deactivate) so the served
    area can change without a code change. A ZIP counts as served when it has an
    ``is_active`` row here. Matched on the first 5 digits of the ZIP. The initial
    list is seeded from the PHS ZIP-code workbook via data migration (all active).
    """

    zip = models.CharField(max_length=5, unique=True, db_index=True)
    borough = models.CharField(max_length=64, blank=True)
    # Deactivate a ZIP without deleting it (keeps the borough/history); only
    # active rows count as in-coverage.
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["zip"]
        verbose_name = "Service ZIP code"
        verbose_name_plural = "Service ZIP codes"

    def __str__(self):
        return f"{self.zip} ({self.borough})" if self.borough else self.zip


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


class ActiveProgram(models.Model):
    """Maps a source Program Name to the Case Category it classifies as.

    Every imported case is classified by looking up its ``program_name`` here
    and reading the matched row's ``case_category`` (Eligibility / Care
    Management / Internal Service / External Service). Agents manage this table
    from Settings > Active Programs, where a program can be added or moved to a
    different case category. See ``api.serializers.derive_case_type``.
    """

    class CaseType(models.TextChoices):
        """The service TYPE a program belongs to, within its case category.

        Internal Services is no longer food-only: housing programs (Dwelling
        Assessment / SOW Development) are internal services of a different TYPE.
        The rule is one governing internal-service case PER TYPE, so a member can
        hold a Food case and a Housing case at once without them competing.

        Active types are tracked in ``ProgramMainCategory.is_active`` (Housing was
        activated to begin processing housing programs); this enum is what the
        routing code binds to.
        """

        FOOD = "food", "Food"
        HOUSING = "housing", "Housing"
        TRANSPORTATION = "transportation", "Transportation"

    class ServiceType(models.TextChoices):
        """The service a program delivers, independent of the population /
        borough / reauthorization variants encoded in ``program_name``."""

        CLINICALLY_APPROPRIATE_MEALS = (
            "clinically_appropriate_meals", "Clinically Appropriate Meals",
        )
        MEDICALLY_TAILORED_MEALS = (
            "medically_tailored_meals", "Medically Tailored Meals (MTM)",
        )
        FOOD_PRESCRIPTIONS = (
            "food_prescriptions", "Food Prescriptions (Voucher / Boxes)",
        )
        # Replaces FOOD_PRESCRIPTIONS for the INTERNAL programmes we deliver
        # ourselves. Both values remain because only the internal rows moved: the
        # external ones are other providers' programmes, named by Unite Us, and
        # renaming those would stop them matching what Unite Us sends us.
        PRODUCE_PRESCRIPTION = (
            "produce_prescription", "Produce Prescription/Voucher",
        )
        SOCIAL_SERVICE_CASE_MANAGEMENT = (
            "social_service_case_management", "Social Service Case Management",
        )
        # Housing (type = HOUSING): the Dwelling Assessment / SOW Development
        # programs. Unite Us sends this verbatim as the case's service_type.
        ENVIRONMENTAL_EXPOSURE_ASSESSMENT = (
            "environmental_exposure_assessment", "Environmental Exposure Assessment",
        )
        # Housing (type = HOUSING): the 15 "Home Remediation - <device> -
        # <borough>" programs (air conditioner, air filtration device,
        # de-humidifier, heater, humidifier x Brooklyn/Manhattan/Queens).
        HOME_EXPENSE_ASSISTANCE_REPAIRS = (
            "home_expense_assistance_repairs", "Home Expense Assistance/Repairs",
        )
        # Housing (type = HOUSING): the "Home Accessibility and Safety
        # Modification - <item> - <borough>" programs (bathroom facilities, grab
        # bars, hand rails, non-skid surfaces). A sibling of the Repairs service --
        # both are WORK ORDERS raised from an assessment, neither can govern.
        ENVIRONMENTAL_MODIFICATIONS_ACCESSIBILITY = (
            "environmental_modifications_accessibility",
            "Environmental Modifications/Accessibility",
        )

    program_name = models.CharField(max_length=255, unique=True, db_index=True)
    main_category = models.CharField(max_length=120, blank=True)
    # ELIGIBILITY / NAVIGATION / Internal Services / External Services, etc.
    case_category = models.CharField(max_length=120, blank=True, db_index=True)
    services_category = models.CharField(max_length=120, blank=True)
    # True when the program name contains the word "Household" (a household
    # pathway). Auto-derived from ``program_name`` on save.
    is_for_household = models.BooleanField(default=False)
    # The service TYPE within the case category (Food / Housing / Transportation).
    # Defaults to Food for backwards compatibility -- every program predating the
    # housing work is food. Internal-service routing is per TYPE: one governing
    # internal-service case for each.
    case_type = models.CharField(
        max_length=20, choices=CaseType.choices, default=CaseType.FOOD
    )
    # The service this program delivers (Settings > Programs). Seeded from the
    # program name by data migration; blank when the program isn't one of the
    # services we deliver. ``db_default`` guards against an omitted-column
    # insert during a deploy window (see ``to_extend`` below).
    # 64, not 40: "environmental_modifications_accessibility" is 41 characters and
    # Django refuses a max_length that cannot hold its own longest choice
    # (fields.E009). Headroom so the next service name does not need a migration.
    service_type = models.CharField(
        max_length=64, choices=ServiceType.choices, blank=True,
        default="", db_default="",
    )

    # THE PARENT PROGRAM: what this service ultimately delivers -- meals, boxes or
    # home remediation. See ProgramParentKind for why this is NOT ProductTypeKind,
    # which it originally reused.
    #
    # It exists so a programme can be matched to the eligibility assessment's
    # result: the assessment says a member is eligible for Medically Tailored Meals
    # or a Produce Prescription, and this is what connects that answer to the
    # programmes that can serve it.
    #
    # BLANK IS MEANINGFUL and common. Only the programmes we deliver a product for
    # carry a value: navigation, case management, housing and every external
    # programme have no parent product, and guessing one would file a housing
    # assessment under "meals".
    # 32, not 16: "home_remediation" is exactly 16 and Django refuses a
    # max_length that cannot hold its own longest choice. Headroom so the next
    # parent program does not need a schema migration.
    parent_program = models.CharField(
        max_length=32, choices=ProgramParentKind.choices, blank=True,
        default="", db_default="", db_index=True,
    )

    # IS THIS A PROGRAMME WE ACTUALLY USE?
    #
    # The table holds every programme Unite Us knows about -- 324 of them, of which
    # 162 are other providers'. That is a lot of noise in a dropdown when only
    # about a third are ours, which is what this flag exists to cut.
    #
    # It is a CLASSIFICATION, not a usage statistic. An inactive programme can
    # still have live cases: External Services took 144 in the last six months, and
    # SCREENING 357. Inactive means "not one of ours to offer", never "nothing is
    # happening here" -- so nothing should use this flag to decide whether a case
    # is real.
    #
    # Defaults TRUE: a programme somebody adds by hand is one they intend to use.
    # Migration 0293 sets it False for the categories outside our three.
    is_active = models.BooleanField(default=True, db_default=True, db_index=True)

    # Opt-in flag (managed from Settings > Programs): this program should be
    # treated as an extension/reauthorization of an existing service. Seeded True
    # for internal-service "Reauthorization: ..." programs by data migration.
    # ``db_default`` guards against an omitted-column insert during a deploy window
    # (see Case.is_extension).
    # DECODED from the programme name, whose last part is the borough
    # ("Home Remediation - Air Conditioner - Queens"). Stored rather than parsed on
    # every read so it can be filtered and grouped in SQL -- and backfilled by
    # migration, so it cannot drift from the name it came from without someone
    # editing the name.
    borough = models.CharField(max_length=40, blank=True, db_index=True)
    to_extend = models.BooleanField(default=False, db_default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["program_name"]
        indexes = [
            models.Index(fields=["case_category"]),
        ]

    def save(self, *args, **kwargs):
        self.is_for_household = "household" in (self.program_name or "").casefold()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.program_name} -> {self.case_category}"


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


class AllowedState(models.Model):
    """A US state we accept clients/cases from (allow-list).

    Editable from Settings (enable/disable) so the served states can change
    without a code change. A row's PRESENCE means the state is enabled; there is
    no row for a disabled state. By default only New York (NY) is enabled.

    Used to warn agents when a client's PRIMARY-address state is not one we take
    clients from: surfaced on the Verification modal (portal) and as a banner in
    the extension. Matching is on the 2-letter USPS code (case-insensitive).
    """

    code = models.CharField(max_length=2, unique=True, db_index=True)  # USPS, e.g. "NY"
    name = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Allowed state"
        verbose_name_plural = "Allowed states"

    def __str__(self):
        return self.code


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
    # When true this credential is the DEDICATED automation session (e.g. a Unite
    # Us service account). Background jobs (exports automation) prefer it so a
    # server-side token refresh never rotates -- and logs out -- a real agent's
    # live browser session. Only one should be flagged at a time.
    for_automation = models.BooleanField(default=False, db_index=True)
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


class ReportExportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ReportExport(models.Model):
    """One background CSV export of an Admin > Reports report. Anchors the async
    export flow (mirrors ImportRun for imports): the Celery task builds the CSV,
    uploads it to S3, and flips the status the UI polls. Also a durable audit of
    who exported what, when. One table for EVERY report, keyed by ``report_key``
    (see api.portal.report_exports.REPORT_BUILDERS)."""

    export_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_key = models.CharField(max_length=64, db_index=True)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=ReportExportStatus.choices,
        default=ReportExportStatus.PENDING, db_index=True,
    )
    file_key = models.CharField(max_length=512, blank=True)  # S3 key under exports/
    filename = models.CharField(max_length=255, blank=True)  # download name
    row_count = models.PositiveIntegerField(null=True, blank=True)  # data rows
    error_log = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="report_exports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["report_key"]),
            models.Index(fields=["status"]),
            models.Index(fields=["requested_by", "created_at"]),
        ]

    def __str__(self):
        return f"ReportExport {self.pk} {self.report_key} ({self.status})"


class UniteUsExportStatus(models.TextChoices):
    """Our processing lifecycle for a requested Unite Us export (distinct from
    Unite Us' own ``state``, which we mirror in ``unite_state``)."""

    REQUESTED = "requested", "Requested"        # POSTed; waiting for Unite Us to generate
    READY = "ready", "Ready"                    # Unite Us state=completed; file available
    IMPORTING = "importing", "Importing"        # downloaded + handed to the import pipeline
    IMPORTED = "imported", "Imported"           # ImportRun finished
    FAILED = "failed", "Failed"


class UniteUsExport(models.Model):
    """One requested Unite Us export (Exports page) tracked through
    request -> poll -> download -> import. Anchors idempotency (never import the
    same export twice) and links to the ImportRun that ingested its CSV."""

    # Unite Us export record UUID (from POST /v1/exports). Unique so the poller
    # is idempotent.
    export_id = models.CharField(max_length=64, unique=True, db_index=True)
    # Unite Us export_type (e.g. "clients", "screeningsv2") and the CSV importer
    # type we map it to (e.g. "screening").
    export_type = models.CharField(max_length=64)
    importer_type = models.CharField(max_length=40, blank=True)
    # Requested reporting window.
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    # Raw Unite Us state (requested/processing/completed/failed/...) and OUR
    # processing status.
    unite_state = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=20, choices=UniteUsExportStatus.choices,
        default=UniteUsExportStatus.REQUESTED, db_index=True,
    )
    # The file_uploads record + filename once generated.
    file_upload_id = models.CharField(max_length=64, blank=True)
    filename = models.CharField(max_length=255, blank=True)
    # Which credential/provider requested it, and who triggered our request.
    provider_id = models.CharField(max_length=64, blank=True)
    triggered_by = models.CharField(max_length=120, blank=True)  # cron | agent:355 | manual
    # The import this export fed into (set once we hand the CSV to the pipeline).
    import_run = models.ForeignKey(
        "ImportRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="uniteus_exports",
    )
    error_log = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"UniteUsExport {self.export_type} {self.export_id} ({self.status})"


# ===========================================================================
# NOTES (append-only)
# ===========================================================================
class NoteSource(models.TextChoices):
    UNITE_US = "unite_us", "Unite Us"
    AGENT = "agent", "Agent"
    SYSTEM = "system", "System"
    GHL = "ghl", "GoHighLevel"


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
    NUTRITIONAL_COUNSELING = "nutritional_counseling", "Nutritional Counseling"
    KITCHEN_SWITCH = "kitchen_switch", "Kitchen Switch"
    CADENCE_SWITCH = "cadence_switch", "Cadence Switch"
    HYBRID_SWITCH = "hybrid_switch", "Hybrid Switch"
    # Agent-facing categories added in the 2026 ticket-category refresh.
    INELIGIBLE_FOR_SERVICE = "ineligible_for_service", "Ineligible for Service"
    MEAL_TYPE_UPDATE = "meal_type_update", "Meal Type Update"
    SOMOS_MEMBER = "somos_member", "SOMOS member"
    SIPPS_MEMBER = "sipps_member", "SIPPS member"
    TRANSFERRED_TO_SCREENING = "transferred_to_screening", "Transferred to Screening"
    OTHER = "other", "Other"
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


class TicketOrigin(models.TextChoices):
    """Who raised the ticket. Persisted on every ticket so the Work Queue can
    tell auto-detected (import / daily-sync) tickets apart from ones an agent
    created by hand. System is the default -- every automated path (the
    ``open_ticket`` helper and the batch management commands) leaves it as-is;
    the manual create path (``WorkQueueView.post``) explicitly sets AGENT."""

    SYSTEM = "system", "System"
    AGENT = "agent", "Agent"


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
    # Who raised the ticket: SYSTEM (import / daily-sync / batch commands) or
    # AGENT (created by hand via the Work Queue). Defaults to SYSTEM so every
    # automated path is correct without a code change; the manual create path
    # sets AGENT explicitly.
    origin = models.CharField(
        max_length=10, choices=TicketOrigin.choices,
        default=TicketOrigin.SYSTEM, db_index=True,
    )
    # VIP flag: an agent marks a ticket VIP (priority handling) when opening it.
    # Defaults to False; surfaced + filterable on the Work Queue.
    vip = models.BooleanField(default=False, db_index=True)
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
    # The agent who created the ticket (manual create). Null for system-raised
    # tickets (origin=SYSTEM); ``created_by_label`` keeps a readable snapshot
    # (agent name, "System", etc.) that survives an agent record change.
    created_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_tickets",
    )
    created_by_label = models.CharField(max_length=255, blank=True)
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
    CONSENT_WITHDRAWN = "consent_withdrawn", "Consent Withdrawn"
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
    # Nutritionist legal sign-off (between Verified and Kitchen Assignment).
    NUTRITIONIST_APPROVED = "nutritionist_approved", "Nutritionist Approved"
    # A Nutritionist paused an individual member (per-member off-ramp).
    NUTRITIONIST_PAUSED = "nutritionist_paused", "Nutritionist Paused"
    # --- Service-delivery lifecycle: one granular type per event. ---
    # Reached the Kitchen Assignment STAGE (awaiting a kitchen) -- NOT an actual
    # kitchen assignment. Distinct from KITCHEN_ASSIGNED so nutritionist approval
    # (which advances into this stage) doesn't read as "Kitchen Assigned".
    AWAITING_KITCHEN = "awaiting_kitchen", "Awaiting Kitchen Assignment"
    KITCHEN_ASSIGNED = "kitchen_assigned", "Kitchen Assigned"
    SERVICE_ACTIVATED = "service_activated", "Service Activated"
    SERVICE_ON_HOLD = "service_on_hold", "Service On Hold"
    SERVICE_RESUMED = "service_resumed", "Service Resumed"
    SERVICE_COMPLETED = "service_completed", "Service Completed"
    SERVICE_CLOSED = "service_closed", "Service Closed"
    SERVICE_CANCELLED = "service_cancelled", "Service Cancelled"
    ENROLLED = "enrolled", "Enrolled"
    # --- Housing dispatch: one granular type per transition, following the
    # verification precedent above, so the History tab reads each step distinctly
    # instead of a pile of generic "Dispatch" rows. ---
    DISPATCH_CREATED = "dispatch_created", "Assessment Order Created"
    DISPATCH_SCHEDULED = "dispatch_scheduled", "Dispatch Scheduled"
    DISPATCH_CONFIRMED = "dispatch_confirmed", "Dispatch Confirmed"
    DISPATCH_VISIT_STARTED = "dispatch_visit_started", "Vendor Visit Started"
    DISPATCH_PENDING_SUBMISSION = "dispatch_pending_submission", "Dispatch Pending Submission"
    DISPATCH_SUBMITTED = "dispatch_submitted", "Dispatch Submitted"
    DISPATCH_VOIDED = "dispatch_voided", "Dispatch Submission Voided"
    DISPATCH_UPLOADED = "dispatch_uploaded", "Dispatch Uploaded to Unite Us"
    DISPATCH_CANCELLED = "dispatch_cancelled", "Dispatch Cancelled"
    # --- Other client-lifecycle events not tied to a stage transition. ---
    TICKET_CREATED = "ticket_created", "New Ticket Created"
    DELIVERY_ADDRESS_CHANGED = "delivery_address_changed", "Delivery Address Changed"
    OUT_OF_ORBIT = "out_of_orbit", "Out of Orbit"
    OUT_OF_RANGE = "out_of_range", "Out of Range"
    # Unite Us migrated this person to a new canonical id; our duplicate Client
    # rows were consolidated onto the survivor (records old id -> new id).
    CLIENT_MIGRATED = "client_migrated", "Client Migrated"
    MEMBER_REACTIVATED = "member_reactivated", "Member Reactivated"
    MEMBER_PAUSED = "member_paused", "Member Paused"
    MEMBER_UNPAUSED = "member_unpaused", "Member Unpaused"
    # --- Import-time eligibility off-ramps (api.services.eligibility) ---
    MEMBER_INELIGIBLE = "member_ineligible", "Member Ineligible"
    MEMBER_ELIGIBILITY_RESTORED = "member_eligibility_restored", "Eligibility Restored"
    MEMBER_SERVICE_INACTIVE = "member_service_inactive", "Service Inactive"
    MEMBER_SERVICE_REACTIVATED = "member_service_reactivated", "Service Reactivated"
    # Recoverable social-care-coverage hold (api.services.eligibility): a fixable
    # coverage gap pauses service (reversible) rather than the hard INELIGIBLE
    # off-ramp; the matching restore fires when coverage recovers.
    MEMBER_COVERAGE_HOLD = "member_coverage_hold", "Coverage Hold"
    MEMBER_COVERAGE_RESTORED = "member_coverage_restored", "Coverage Restored"
    # The GOVERNING internal-service case for a program changed (a new case was
    # created, a case was approved and superseded the prior one, or the prior
    # governing case closed). Recorded once per actual change by the case
    # reconcile (api.services.lifecycle).
    MEMBER_GOVERNING_CASE_CHANGED = "member_governing_case_changed", "Governing Case Changed"
    # Governing case switched product KIND (meals<->boxes) to an authorized case:
    # the household was paused + requeued for a new kitchen assignment
    # (api.services.lifecycle). Recorded once per switch by the case reconcile.
    MEMBER_PROGRAM_SWITCHED = "member_program_switched", "Program Switched"
    # Governing case switched household SCOPE (household<->individual). Needs
    # Customer Service review via a CaseMismatchFlag; recorded once per switch by
    # the case reconcile (api.services.lifecycle).
    MEMBER_CASE_MISMATCH = "member_case_mismatch", "Case Mismatch"
    # A new governing case carried the household back into service, but this
    # member could NOT be returned with it (see
    # timeline.event_for_member_service_carry_blocked). Previously this failed
    # silently, leaving the household Service Active with a kitchen, no cadence
    # and no deliveries, discoverable only by auditing the Data page.
    MEMBER_SERVICE_CARRY_BLOCKED = (
        "member_service_carry_blocked", "Not Returned To Service",
    )
    HOUSEHOLD_MEMBER_ADDED = "household_member_added", "Household Member Added"
    HOUSEHOLD_MEMBER_REMOVED = "household_member_removed", "Household Member Removed"
    PRODUCT_TYPE_CHANGED = "product_type_changed", "Product Type Changed"
    # A member's dietary data (restrictions / allergies / menu / meal category)
    # was edited. The specific before -> after fields are in metadata["changes"].
    DIETARY_CHANGED = "dietary_changed", "Dietary Info Updated"
    # --- Legacy coarse types: retained so existing rows stay valid; no longer
    # emitted by the timeline service (a data migration remaps old rows). ---
    VERIFICATION = "verification", "Verification"
    SERVICE = "service", "Service"
    # A pending verification request was disregarded (dismissed) by an agent.
    VERIFICATION_DISREGARDED = "verification_disregarded", "Verification Disregarded"


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
    # Free-text reason/detail -- can be long (e.g. a hold reason with a Unite Us
    # case URL), so store it unbounded rather than a varchar(255).
    subtitle = models.TextField(blank=True)
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
# Member / household warnings (care-management flags)
# ---------------------------------------------------------------------------
class WarningSeverity(models.TextChoices):
    """Severity band for a member/household warning; drives the UI colour.
    Ordered vocabulary -- add more levels here without touching the checks."""

    RED = "red", "Red"
    ORANGE = "orange", "Orange"


class WarningStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RESOLVED = "resolved", "Resolved"


class MemberWarning(models.Model):
    """A persisted snapshot of a detected member/household problem.

    Detection logic lives in ``api.services.warnings`` (the rule registry);
    ``sync_household_warnings`` reconciles this table from the evaluator so it
    can power the profile header and the Care Management page without
    recomputing across the whole DB. One row per (client, code): re-evaluation
    UPSERTS (reactivates + refreshes ``last_seen_at``), and a warning that is no
    longer detected is marked RESOLVED (kept for the audit trail, not deleted).

    ``client`` is the member the warning attaches to (the household primary for
    household-scope warnings; the affected member for member-scope ones).
    ``enrollment`` is the household context used to group rows on the Care
    Management page (one row per household).
    """

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="warnings"
    )
    enrollment = models.ForeignKey(
        "EnrollmentVerification", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="warnings",
    )
    code = models.CharField(max_length=64)
    severity = models.CharField(max_length=16, choices=WarningSeverity.choices)
    scope = models.CharField(max_length=16)  # "household" | "member"
    title = models.CharField(max_length=120)
    detail = models.TextField(blank=True)
    # Deep-link / diagnostic references (case_ids, kitchen_id, end_date, ...).
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=WarningStatus.choices,
        default=WarningStatus.ACTIVE, db_index=True,
    )
    first_detected_at = models.DateTimeField(auto_now_add=True)
    # Last time the check DETECTED this problem (not touched on resolve).
    last_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-severity", "first_detected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                name="uniq_member_warning_client_code",
            )
        ]
        indexes = [
            models.Index(fields=["status", "severity"]),
            models.Index(fields=["enrollment", "status"]),
            models.Index(fields=["code"]),
        ]

    def __str__(self):
        return f"{self.code} ({self.status}) for {self.client_id}"


# ---------------------------------------------------------------------------
# Case mismatch flags (governing-case Household<->Individual scope switch)
# ---------------------------------------------------------------------------
class CaseMismatchType(models.TextChoices):
    """Direction of a governing-case household-scope switch."""

    HOUSEHOLD_TO_INDIVIDUAL = "household_to_individual", "Household \u2192 Individual"
    INDIVIDUAL_TO_HOUSEHOLD = "individual_to_household", "Individual \u2192 Household"


class CaseMismatchStatus(models.TextChoices):
    OPEN = "open", "Open"
    DISMISSED = "dismissed", "Dismissed"


class CaseMismatchFlag(models.Model):
    """A governing-case Household<->Individual scope switch that needs Customer
    Service review.

    Created by the case reconcile (``api.services.lifecycle``) when the client's
    governing internal-service case changes its ``household_type``. Unlike a
    :class:`MemberWarning` (which auto-resolves when the condition clears), a
    Case Mismatch flag is a manual work item: it stays OPEN until Customer
    Service dismisses it, and never re-locks on a switch back. Dismissing a
    Household->Individual flag also clears the ``pause_locked`` pin on the
    household's additional members (they were auto-paused + pinned on the
    switch). Surfaced on the Care Management -> Case Mismatch tab.

    ``client`` is the household PRIMARY. De-duped on ``(client, new_case_id)`` so
    a re-import never stacks duplicate flags (and a dismissed flag is not
    re-created).
    """

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="case_mismatch_flags"
    )
    enrollment = models.ForeignKey(
        "EnrollmentVerification", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="case_mismatch_flags",
    )
    mismatch_type = models.CharField(max_length=32, choices=CaseMismatchType.choices)
    previous_case_id = models.CharField(max_length=64, blank=True)
    new_case_id = models.CharField(max_length=64, blank=True)
    previous_household_type = models.CharField(max_length=12, blank=True)
    new_household_type = models.CharField(max_length=12, blank=True)
    detail = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=CaseMismatchStatus.choices,
        default=CaseMismatchStatus.OPEN, db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_by = models.CharField(max_length=255, blank=True)
    dismiss_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "new_case_id"],
                name="uniq_case_mismatch_client_new_case",
            )
        ]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["enrollment", "status"]),
        ]

    def __str__(self):
        return f"{self.mismatch_type} ({self.status}) for {self.client_id}"


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


class MealPlan(models.Model):
    """A named meal plan, managed from Settings. A simple catalog entry
    (name + description + active flag) referenced elsewhere in the app."""

    meal_plan_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    # Disabled plans are hidden from selection but kept for historical records.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ClientTagColor(models.TextChoices):
    """Preset colours for a client tag, roughly ordered by implied importance
    (red = most urgent). The value is a stable slug; the label is shown in
    Settings and the frontend maps the slug to a colour swatch."""

    RED = "red", "Red"
    ORANGE = "orange", "Orange"
    YELLOW = "yellow", "Yellow"
    GREEN = "green", "Green"
    BLUE = "blue", "Blue"
    PURPLE = "purple", "Purple"
    GRAY = "gray", "Gray"


class ClientTag(models.Model):
    """A colour-coded label managed from Settings and attached to clients/members
    (see ``Client.tags``). A simple catalog entry: name + colour."""

    client_tag_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=120, unique=True)
    color = models.CharField(
        max_length=20, choices=ClientTagColor.choices,
        default=ClientTagColor.BLUE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_color_display()})"


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
    # Short code used in the human-readable PO number (e.g. "HICK" ->
    # PO-MEALS-2026-W32-THU-HICK). Falls back to an auto "K01"-style code when
    # blank, so PO naming keeps working before abbreviations are configured.
    abbreviation = models.CharField(max_length=12, blank=True)
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
    # Delivery cadences this kitchen takes orders for. Configuration only for
    # now (surfaced in Settings); not yet enforced in kitchen assignment.
    cadences = models.ManyToManyField(
        "Cadence",
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


class PartnerScope(models.TextChoices):
    """What a delivery partner's credential is allowed to do. Kept coarse: the
    partner API only ever ingests proof of delivery."""

    POD_STATUS = "pod:status", "Update delivery status"
    POD_PHOTO = "pod:photo", "Upload proof-of-delivery photos"


def default_partner_scopes():
    return [PartnerScope.POD_STATUS, PartnerScope.POD_PHOTO]


class DeliveryCompanyApiClient(models.Model):
    """INBOUND API credential for a delivery company, so their backend can push
    proof of delivery to us (see docs/delivery_partner_api_plan.md).

    One credential per company. ``client_id`` is public and identifies them;
    the secret is only ever stored HASHED and is shown once at creation. A
    rotation keeps the previous secret working until
    ``previous_secret_expires_at`` so the vendor can redeploy without an outage.

    This is deliberately NOT ``DeliveryCompanyIntegration``: that modelled the
    (now removed) outbound "how they receive orders" direction.
    """

    delivery_company = models.OneToOneField(
        DeliveryCompany, on_delete=models.CASCADE, related_name="api_client"
    )
    # Public identifier, e.g. "ccpod_qari_a1b2c3d4" -- greppable in logs and by
    # secret scanners, and safe to show in the UI.
    client_id = models.CharField(max_length=64, unique=True, db_index=True)
    secret_hash = models.CharField(max_length=128)
    # Rotation overlap: the superseded secret stays valid until it lapses.
    previous_secret_hash = models.CharField(max_length=128, blank=True)
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=default_partner_scopes, blank=True)
    created_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_delivery_api_clients",
    )
    # Optional hardening: when set, only these IPs/CIDRs may authenticate.
    allowed_ips = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["delivery_company__name"]

    def __str__(self):
        return f"{self.delivery_company} ({self.client_id})"

    @property
    def is_active(self):
        now = timezone.now()
        if self.revoked_at is not None:
            return False
        return not (self.expires_at and self.expires_at <= now)


class PartnerAccessToken(models.Model):
    """A short-lived OPAQUE bearer token issued from a client_id/secret exchange.

    Opaque (random, stored hashed) rather than a JWT on purpose: the project's
    DEFAULT_AUTHENTICATION_CLASSES include JWT authenticators, so a partner JWT
    signed with the shared key would authenticate against the whole CRM. An
    opaque token is meaningless to those authenticators, and revoking it is a
    single row update.
    """

    client = models.ForeignKey(
        DeliveryCompanyApiClient, on_delete=models.CASCADE, related_name="tokens"
    )
    # sha256 of the token; the raw value is returned once and never stored.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_ip = models.CharField(max_length=64, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["client", "expires_at"])]

    def __str__(self):
        return f"Partner token for {self.client_id} (exp {self.expires_at:%Y-%m-%d %H:%M})"

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()


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


class DeliveryStatusSource(models.TextChoices):
    """How a delivery's outcome reached us.

    Worth recording because the two channels have different reliability: a CSV is a
    file someone uploaded, possibly days late and possibly the wrong week, while the
    API is the courier's own system reporting as it happens. "Where did this status
    come from?" is the first question when a delivery looks wrong.
    """

    CSV = "csv", "CSV import"
    API = "api", "Partner API"


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
    # Where the CURRENT status came from. Set alongside the status itself, so a
    # delivery whose outcome arrived by CSV can be told from one the courier's API
    # reported -- including for a status-only report that carries no photo, which a
    # field on the proof could never answer.
    status_source = models.CharField(
        max_length=8, choices=DeliveryStatusSource.choices, blank=True,
    )

    # WHO DELIVERED IT, BY WHICH ROUTE, AND WHAT THEY SAID.
    #
    # These live here as well as on DeliveryOrderProof, and that duplication is the
    # fix for a real defect rather than an oversight: they were proof-ONLY fields,
    # and a proof row exists only when a photo does. A report with no photos --
    # which is most of them; one real USP file updated 184 orders and created ZERO
    # proofs -- parsed the driver and route and then discarded both.
    #
    # They are facts about the DELIVERY, not about a photograph of it, so the order
    # is where they belong. The proof keeps its own copies, because a specific photo
    # can legitimately come from a different driver on a redelivery.
    delivery_driver = models.CharField(max_length=255, blank=True)
    delivery_route = models.CharField(max_length=255, blank=True)
    delivery_note = models.TextField(blank=True)
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
    # Proof of delivery now lives in the DeliveryOrderProof child model (one row
    # per image, sourced from the per-company delivery reports). See
    # docs/proof_of_delivery_plan.md.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["purchase_order"]),
            models.Index(fields=["status"]),
            models.Index(fields=["member"]),
            models.Index(fields=["expected_delivery_date"]),
            # "Last delivered" lookups (latest delivered_at per member).
            models.Index(fields=["member", "delivered_at"]),
        ]

    def __str__(self):
        return f"DeliveryOrder {self.delivery_order_id} ({self.get_status_display()})"


class DeliveryOrderProof(models.Model):
    """A single proof-of-delivery image for a :class:`DeliveryOrder`, ingested
    from a delivery company's per-PO delivery report (CSV).

    The image binary is copied into OUR S3 (the vendor's Photos URLs are
    short-lived signed CloudFront links that expire), and this row records the
    stored object + the delivery metadata from the report row. A delivery order
    can have many proofs (multiple photos, and/or redeliveries over time).
    ``content_hash`` (sha256 of the bytes) makes re-imports idempotent and
    de-dupes the same image seen across reports.
    """

    delivery_order = models.ForeignKey(
        DeliveryOrder, on_delete=models.CASCADE, related_name="proofs"
    )
    # Where the image lives in OUR bucket (authoritative), + a stored URL.
    s3_key = models.CharField(max_length=500)
    file_url = models.URLField(max_length=1000, blank=True)
    # The original (expiring) vendor URL we fetched from -- kept for audit only.
    source_url = models.TextField(blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    # Delivery metadata snapshotted from the report row.
    delivery_company = models.ForeignKey(
        DeliveryCompany, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="delivery_order_proofs",
    )
    driver = models.CharField(max_length=255, blank=True)
    route_id = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # The source report filename / import identifier this image came from.
    source_report = models.CharField(max_length=255, blank=True)
    # Which channel delivered this proof. source_report names the FILE for a CSV;
    # this names the CHANNEL, which is the part that is comparable across the two.
    status_source = models.CharField(
        max_length=8, choices=DeliveryStatusSource.choices, blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_order", "content_hash"],
                name="unique_delivery_order_proof_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["delivery_order"]),
        ]

    def __str__(self):
        return f"Proof for delivery order {self.delivery_order_id} ({self.s3_key})"


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


class PurchaseOrderNote(models.Model):
    """An agent note on a Purchase Order (Orders page). Note history: each row is
    an immutable entry stamped with the author agent + when it was written."""

    purchase_order = models.ForeignKey(
        "PurchaseOrder", on_delete=models.CASCADE, related_name="notes"
    )
    # The agent who wrote the note; ``author_name`` is a snapshot so the row
    # stays readable even if the agent record changes/clears.
    author_agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchase_order_notes",
    )
    author_name = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["purchase_order", "created_at"])]

    def __str__(self):
        return f"Note on PO {self.purchase_order_id} by {self.author_name or 'system'}"


class TicketActivityAction(models.TextChoices):
    """What happened to a ticket, for the ticket activity/history feed."""

    CREATED = "created", "Created"
    ASSIGNED = "assigned", "Assigned"
    UNASSIGNED = "unassigned", "Unassigned"
    STATUS_CHANGED = "status_changed", "Status Changed"
    RESOLVED = "resolved", "Resolved"
    REOPENED = "reopened", "Reopened"
    NOTE_ADDED = "note_added", "Note Added"
    SEVERITY_CHANGED = "severity_changed", "Severity Changed"
    VIP_CHANGED = "vip_changed", "VIP Changed"


class TicketActivity(models.Model):
    """A chronological activity/history entry for a Ticket -- one row per action
    (created, assigned, status changed, note added, resolved, ...), so the Work
    Queue can show a full timestamped history of what happened to a ticket and
    who did it. Explicitly written by the ticket actions (see
    :func:`api.services.tickets.log_ticket_activity`)."""

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="activities"
    )
    action = models.CharField(max_length=24, choices=TicketActivityAction.choices)
    # WHO did it: an optional structured Agent link + a readable snapshot label
    # ("Casey CS", "System", "system:cancelled-reconcile", ...).
    actor_agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ticket_activities",
    )
    actor_label = models.CharField(max_length=255, blank=True)
    # Human-readable one-liner (e.g. "Status: Open -> In Progress", a note
    # excerpt) plus structured context for the frontend.
    detail = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["ticket", "created_at"])]

    def __str__(self):
        return f"Ticket {self.ticket_id} {self.action} @ {self.created_at:%Y-%m-%d %H:%M}"


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


class EnrollmentAnalytics(models.Model):
    """Denormalized, per-MEMBER read model powering the Administration > Data
    page (arbitrary-field filtering + exports for the data team).

    One row per :class:`Client` (every member, including those with no enrollment
    / no internal-service case), flattening every Data-page filter field for the
    member's active/governing enrollment -- including DERIVED values (delivery
    statuses, last-delivered) and
    MULTI-VALUED ones (allergies/conditions/medications/eligible-services, stored
    as arrays with GIN indexes) -- so the Data page never joins the live 12-table
    graph. Rebuilt on a schedule (~hourly); see services/enrollment_analytics.py
    and docs/analytics-architecture.md. NOT the source of truth -- always
    reproducible from the operational tables.
    """

    # Identity / joins. Grain = MEMBER (one row per Client), so EVERY member is
    # represented -- including those with no enrollment / no internal-service case
    # (company_status = no_case). enrollment_id references the member's active/
    # governing enrollment when they have one (else null).
    client = models.OneToOneField(
        "Client", on_delete=models.CASCADE,
        related_name="analytics", primary_key=True,
    )
    enrollment_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    household_id = models.UUIDField(null=True, blank=True, db_index=True)
    case_id = models.UUIDField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    stage = models.CharField(max_length=25, blank=True, db_index=True)

    # Display / export identity.
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    medicaid_id = models.CharField(max_length=64, blank=True)

    # --- scalar filter columns (btree via db_index) ---
    dob = models.DateField(null=True, blank=True, db_index=True)
    # Unite Us's own created date for the person. Kept for reference/export, but
    # NOT what "Member Created" filters -- Unite Us migrated ~80% of this
    # population in over a few days, so it clusters uselessly. See Client.
    member_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # When the member first reached OUR system (Client.client_added_at). This is
    # what the Data page's Member-Created range filters on.
    member_added_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # How the member first reached us (extension vs import). ``db_default`` for the
    # same omitted-column reason as Client.source.
    source = models.CharField(max_length=20, blank=True, db_index=True, db_default="")
    care_coordinator = models.CharField(max_length=255, blank=True, db_index=True)
    primary_care_coordinator = models.CharField(max_length=255, blank=True)
    cadence = models.CharField(max_length=40, blank=True, db_index=True)
    kitchen_id = models.UUIDField(null=True, blank=True, db_index=True)
    kitchen_name = models.CharField(max_length=255, blank=True)
    menu_type = models.CharField(max_length=120, blank=True, db_index=True)

    # Derived delivery fields.
    current_delivery_status = models.CharField(max_length=30, blank=True, db_index=True)
    last_po_delivery_status = models.CharField(max_length=30, blank=True)
    last_delivered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Delivery date of the member's most-recent PO-backed delivery order,
    # REGARDLESS of delivered status (distinct from last_delivered_at, which is
    # only the last order marked delivered). Powers the "Last PO Delivery Date"
    # filter.
    last_po_delivery_date = models.DateTimeField(null=True, blank=True, db_index=True)
    # True when the member has EVER been included in a generated Purchase Order
    # (has a DeliveryOrder line tied to a PO) -- regardless of delivery status.
    in_any_po = models.BooleanField(default=False, db_index=True)
    # Enrollment-grain verification flags (own OR household enrollment), so the
    # Data page can match the Verification page's operational queue exactly:
    #   has_pending_verification_enrollment -> ANY enrollment at pending_verification
    #   has_verified_enrollment             -> ANY governing enrollment verified
    # (the scalar verification_state below stays the member's GOVERNING-enrollment
    # fact, for display/analytics).
    has_pending_verification_enrollment = models.BooleanField(default=False, db_index=True)
    has_verified_enrollment = models.BooleanField(default=False, db_index=True)
    #   has_never_requested_verification -> open governing IS case (primary scope)
    #     but the member never entered verification (not verified, no pending
    #     enrollment, never requested). Powers the Data page's "Never Requested".
    has_never_requested_verification = models.BooleanField(default=False, db_index=True)

    # Coverage.
    insurance_status = models.CharField(max_length=20, blank=True, db_index=True)
    insurance_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    social_status = models.CharField(max_length=20, blank=True, db_index=True)
    social_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Attestation (dates nullable -- backfilled from CRM later).
    attestation_status = models.CharField(max_length=20, blank=True, db_index=True)
    attestation_requested_at = models.DateTimeField(null=True, blank=True)
    attestation_completed_at = models.DateTimeField(null=True, blank=True)

    # Screening / eligibility assessment.
    has_screening = models.BooleanField(default=False, db_index=True)
    screening_at = models.DateTimeField(null=True, blank=True)
    # Name of the Unite Us facilitator who performed the latest screening
    # (Screening.facilitator_id -> UniteUsAgent.employee_id -> name). Powers the
    # Data page "Screening Agent" filter. Blank when no screening / unresolved.
    screening_agent = models.CharField(max_length=255, blank=True, db_index=True)
    has_eligibility_assessment = models.BooleanField(default=False, db_index=True)
    eligibility_assessment_at = models.DateTimeField(null=True, blank=True)

    # Verification provenance (mirrors the "System" fallback used on the pages).
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    verified_by_name = models.CharField(max_length=255, blank=True)

    # Governing internal-service case snapshot.
    case_type = models.CharField(max_length=20, blank=True, db_index=True)
    case_status = models.CharField(max_length=25, blank=True, db_index=True)
    auth_status = models.CharField(max_length=20, blank=True, db_index=True)
    # Governing case's authorization window (approved window, else requested).
    auth_start_date = models.DateTimeField(null=True, blank=True, db_index=True)
    auth_end_date = models.DateTimeField(null=True, blank=True, db_index=True)
    case_opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    program_name = models.CharField(max_length=255, blank=True, db_index=True)

    # --- Members-parity criteria (populated from MemberListSerializer output so
    # the Data page numbers match the Members page exactly). ---
    eligibility = models.CharField(max_length=20, blank=True, db_index=True)
    verification_state = models.CharField(max_length=40, blank=True, db_index=True)
    program_status = models.CharField(max_length=40, blank=True, db_index=True)
    # Data-team roll-up: one bucket per member (active / pending / unable / paused
    # / closed / no_case). Derived in the builder; see _company_status.
    company_status = models.CharField(max_length=20, blank=True, db_index=True)
    # Nutritionist review status (pending / approved) and the delivery company on
    # the member's latest delivery order.
    nutritionist_status = models.CharField(max_length=20, blank=True, db_index=True)
    delivery_company = models.CharField(max_length=255, blank=True, db_index=True)
    lead_source = models.CharField(max_length=120, blank=True, db_index=True)
    team = models.CharField(max_length=120, blank=True, db_index=True)
    service_type = models.CharField(max_length=20, blank=True, db_index=True)
    program_type = models.CharField(max_length=20, blank=True, db_index=True)
    out_of_orbit = models.BooleanField(default=False, db_index=True)
    out_of_range = models.BooleanField(default=False, db_index=True)
    paused = models.BooleanField(default=False, db_index=True)
    pause_type = models.CharField(max_length=20, blank=True)
    # When the member entered their current paused state -- member-status pause
    # (MemberDietaryProfile.status_changed_at) or, for an On-Hold enrollment, the
    # enrollment's stage_at. Null when not paused. Powers the "Pause Date" filter.
    pause_date = models.DateTimeField(null=True, blank=True, db_index=True)
    verified_by_id_str = models.CharField(max_length=64, blank=True, db_index=True)
    requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    case_closed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # --- multi-valued (array + GIN) ---
    allergies = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    medical_conditions = ArrayField(models.CharField(max_length=128), default=list, blank=True)
    medications = ArrayField(models.CharField(max_length=128), default=list, blank=True)
    eligible_services = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    # The member's phone numbers (primary first). Exported one-per-column on the
    # Data page CSV ("Phone 1", "Phone 2", ...).
    phone_numbers = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    # Codes of EVERY ticket type the member has ever had, whatever the ticket's
    # status -- the Data page filter is analytical ("who has ever had this"),
    # unlike the Members page's work-queue filter which binds to OPEN tickets.
    ticket_types = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    # When this row was last rebuilt (freshness watermark).
    refreshed_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [
            GinIndex(fields=["allergies"], name="ea_allergies_gin"),
            GinIndex(fields=["medical_conditions"], name="ea_conditions_gin"),
            GinIndex(fields=["medications"], name="ea_medications_gin"),
            GinIndex(fields=["eligible_services"], name="ea_elig_services_gin"),
            GinIndex(fields=["tags"], name="ea_tags_gin"),
            GinIndex(fields=["ticket_types"], name="ea_ticket_types_gin"),
        ]

    def __str__(self):
        return f"EnrollmentAnalytics(client={self.client_id})"


class AnalyticsRebuildRun(models.Model):
    """One execution of the EnrollmentAnalytics rebuild task/command. Lets the Data
    page show when the read model was last REBUILT by the job (scheduled or the
    manual button) -- distinct from EnrollmentAnalytics.refreshed_at, which any
    single-row live upsert (a member edit) bumps and so isn't the task's run time."""

    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    rows = models.IntegerField(default=0)
    pruned = models.IntegerField(default=0)
    # How the rebuild was launched: "scheduled" (Celery beat), "manual" (Data page
    # button / ad-hoc task) or "cli" (management command). Free text; informational.
    trigger = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"AnalyticsRebuildRun({self.started_at:%Y-%m-%d %H:%M} {self.trigger})"


# ── Dispatch: work sent out to external vendors ───────────────────────────────
# Housing services are executed by VENDORS at the member's home, not by us. The
# dispatch domain is the record of that work: an order, the visit, the evidence
# produced, and the manual upload of that evidence back to Unite Us.
#
# "Work order" is NOT the head name on purpose -- in this project it already means
# a Home Remediation case specifically (api/services/housing.py, the Cases tab), so
# promoting it to the generic term would give one word two meanings. The head is
# DispatchOrder; a Home Remediation case remains a "work order".
#
# See docs/housing-assessment-order-plan.md.


class Vendor(models.Model):
    """A company that executes housing work at a member's home.

    Deliberately NOT ``DeliveryCompany`` (which is "a delivery company/vendor that
    transports meal orders") and NOT ``Provider`` (the Unite Us organization).
    Assessing a dwelling and driving meals are different relationships with
    different people; sharing a table would make every existing delivery query
    ambiguous.

    Created by an admin in CRM Settings, which also provisions exactly one
    :class:`VendorUser` with ``is_admin`` -- that person then creates their own
    staff in the vendor portal.
    """

    vendor_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    website = models.URLField(max_length=255, blank=True)
    # The mark-up we add to THIS vendor's prices when billing Unite Us. NULL means
    # "use the global default" rather than 0 -- so a vendor nobody has set a fee for
    # inherits the house rate, and changing that rate moves them with it. Storing a
    # copy of the default would leave every vendor stale the day it changed.
    admin_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )

    # The company's logo, for their own portal and for the assessment PDFs they
    # produce. An S3 KEY, not a URL: URLs are presigned and expire, so storing one
    # would leave a dead link in every document that had been generated with it.
    logo_s3_key = models.CharField(max_length=500, blank=True)
    logo_updated_at = models.DateTimeField(null=True, blank=True)
    # The NORMALISED logo's pixel dimensions, so document layout can reserve the
    # right box from its aspect ratio without fetching and decoding the image for
    # every invoice.
    logo_width = models.PositiveIntegerField(null=True, blank=True)
    logo_height = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class VendorUser(models.Model):
    """A person at a vendor company. NOT a CRM user.

    Kept separate from ``Agent`` on purpose: a vendor must never reach CRM
    endpoints. Every vendor query is scoped to ``vendor``, following the partner
    API's principal, which exists "to carry the company every query must be scoped
    to" -- the boundary lives in the principal, not in a filter remembered at each
    call site.

    ``is_admin`` marks the single bootstrap user CRM Settings provisions. That
    person creates the rest of their staff in the vendor portal; CRM retains only
    the ability to reset THIS user's password.
    """

    vendor_user_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="users")
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=40, blank=True)
    # Django's hasher; never a plaintext or reversible value.
    password = models.CharField(max_length=255, blank=True)
    is_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vendor__name", "name"]
        constraints = [
            # CRM Settings provisions exactly ONE admin per vendor. Enforced here
            # rather than in the view, so a second cannot arrive via the shell or
            # a future endpoint.
            models.UniqueConstraint(
                fields=["vendor"], condition=models.Q(is_admin=True),
                name="one_admin_user_per_vendor",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.vendor.name})"


class VendorAccessToken(models.Model):
    """A short-lived OPAQUE bearer token for a logged-in vendor user.

    Mirrors :class:`PartnerAccessToken` and for the same reason, restated because
    it is the single most important decision on this surface: the project's
    DEFAULT_AUTHENTICATION_CLASSES include JWT authenticators, so a vendor JWT
    signed with the shared key would authenticate against the WHOLE CRM. An
    opaque random token is meaningless to those authenticators, and revoking it is
    one row update -- which is what makes "this vendor lost their phone" a
    two-second fix.

    Differs from the partner token in one way that matters: the principal is a
    PERSON, not a company credential, so the token carries the user and the
    vendor. Every query is scoped to the vendor; the user is who did it.
    """

    vendor_user = models.ForeignKey(
        VendorUser, on_delete=models.CASCADE, related_name="tokens"
    )
    # sha256 of the token; the raw value is returned once and never stored.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_ip = models.CharField(max_length=64, blank=True)
    # Free text from the login request, for "sign out my other devices" and for
    # telling one shared tablet from another.
    device_label = models.CharField(max_length=120, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["vendor_user", "expires_at"])]

    def __str__(self):
        return f"VendorAccessToken({self.vendor_user_id})"

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()


class DispatchKind(models.TextChoices):
    ASSESSMENT = "assessment", "Dwelling Assessment"
    REMEDIATION = "remediation", "Home Remediation"


class DispatchStatus(models.TextChoices):
    """A single LINEAR chain -- not independent flags."""

    PENDING_SCHEDULE = "pending_schedule", "Pending Schedule"
    CONFIRMED = "confirmed", "Confirmed"
    PENDING_SUBMISSION = "pending_submission", "Pending Submission"
    SUBMITTED = "submitted", "Submitted"
    UPLOADED = "uploaded", "Uploaded"
    CANCELLED = "cancelled", "Cancelled"


class DispatchReferralType(models.TextChoices):
    MOBILITY = "mobility", "Mobility"
    VENTILATION = "ventilation", "Ventilation"
    COMBINED = "combined", "Combined"


class DispatchOrder(models.Model):
    """A unit of work dispatched to a vendor.

    ONE model with a ``kind`` rather than separate assessment and remediation
    models, because the two share everything that matters -- the appointment, the
    visit, signatures, photos, documents, findings and the Unite Us upload. Two
    models would mean building that layer twice.

    An ASSESSMENT order is created once per member by an agent running the wizard
    on the member profile. Each REMEDIATION order is created automatically from an
    imported Home Remediation case and parented to that member's assessment order,
    so every remediation hangs off the assessment that found the need for it.

    Append-only in spirit: nothing here is reconciled, rebuilt or superseded-in-
    place. A correction is a new :class:`DispatchSubmission`, never an edit.
    """

    dispatch_order_id = models.UUIDField(
        # CLIENT-generated: an offline vendor retries, and without an id the
        # client chose, a retry creates a DUPLICATE order.
        primary_key=True, default=uuid.uuid4, editable=False,
    )
    kind = models.CharField(max_length=20, choices=DispatchKind.choices, db_index=True)
    # Remediation orders hang off their assessment. Null for an assessment.
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True,
        related_name="children",
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="dispatch_orders"
    )
    # The Unite Us case this order serves: the Dwelling Assessment case for an
    # assessment, the Home Remediation case for a remediation.
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_orders",
    )
    # Labelled dwelling cases, e.g. {"primary": "<case_id>", "secondary": "..."}.
    # A second assessment becomes another LABEL here rather than a second order.
    dwellings = models.JSONField(default=dict, blank=True)

    # The borough the work happens in, parsed from the case's program name. The
    # ITEMS themselves live on DispatchItem: a work order is a BATCH, so it has
    # many items and cannot carry one.
    location = models.CharField(max_length=120, blank=True)

    status = models.CharField(
        max_length=20, choices=DispatchStatus.choices,
        default=DispatchStatus.PENDING_SCHEDULE, db_index=True,
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, null=True, blank=True,
        related_name="dispatch_orders",
    )
    created_by = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_orders_created",
    )

    # --- step 1 of the wizard: captured ON the order ------------------------
    # NOT read live from the client. A dwelling assessment is about a specific
    # PROPERTY, so a later edit to the member's address must not rewrite where a
    # completed assessment happened. Verified ONCE, on the assessment; remediation
    # orders inherit it through `parent` (see `service_address`).
    contact_phone = models.CharField(max_length=40, blank=True)
    contact_phone_type = models.CharField(max_length=10, blank=True)  # mobile/landline
    contact_email = models.EmailField(blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    address_city = models.CharField(max_length=120, blank=True)
    address_state = models.CharField(max_length=40, blank=True)
    address_zip = models.CharField(max_length=20, blank=True)
    address_formatted = models.CharField(max_length=500, blank=True)
    address_place_id = models.CharField(max_length=255, blank=True)
    address_lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    address_lng = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    address_notes = models.TextField(blank=True)

    # --- step 2 of the wizard ------------------------------------------------
    # Functional, not a label: it selects which questionnaire the member is asked,
    # and COMBINED means both.
    referral_type = models.CharField(
        max_length=20, choices=DispatchReferralType.choices, blank=True,
    )
    # VERBAL consent for the vendor to contact the member. Two booleans behind one
    # checkbox because SMS and calls are separable and a member may later object to
    # one; that distinction cannot be reconstructed from a single flag. GDPR puts
    # the burden on us to DEMONSTRATE consent, hence method/when/who.
    consent_to_call = models.BooleanField(default=False)
    consent_to_text = models.BooleanField(default=False)
    consent_method = models.CharField(max_length=20, blank=True, default="verbal")
    consent_captured_at = models.DateTimeField(null=True, blank=True)
    consent_captured_by = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_consents_captured",
    )
    # Agent sign-off that the Enhanced Care Management case is billed. A plain
    # attestation for now; showing the underlying ECM case is deferred until the
    # Unite Us case details/invoice reveal whether "billed" is derivable at all.
    ecm_billed_confirmed = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Rule 1: one assessment order per member. A partial unique index, so
            # remediation orders are unaffected.
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(kind="assessment"),
                name="one_assessment_order_per_client",
            ),
            # NOTE there is deliberately no per-case constraint on remediation
            # orders any more. A work order is a BATCH of items drawn from several
            # cases, and one case's item may be re-dispatched in a later batch if
            # the first was cancelled. Uniqueness now belongs to DispatchItem,
            # which IS one per case.
        ]
        indexes = [
            models.Index(fields=["client", "kind"]),
            models.Index(fields=["vendor", "status"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {str(self.dispatch_order_id)[:8]}"

    @property
    def service_address(self):
        """The address the work happens at.

        A remediation order INHERITS from its assessment rather than copying, so a
        child can never drift from the address that was verified once.
        """
        if self.kind == DispatchKind.REMEDIATION and self.parent_id:
            return self.parent.address_formatted or self.parent.address_line1
        return self.address_formatted or self.address_line1

    @property
    def service_contact(self):
        """``(phone, phone_type, notes)`` for reaching the member about this order.

        Inherited by a remediation order for the same reason as the address: the
        wizard collects contact details ONCE, on the assessment, so a work order
        has none of its own. Without this an installer opens their job and sees no
        number to ring -- which is exactly what the first live vendor-API call
        showed.
        """
        source = self._contact_source()
        return source.contact_phone, source.contact_phone_type, source.address_notes

    def _contact_source(self):
        """The order the member's contact details and consent actually live on."""
        if self.kind == DispatchKind.REMEDIATION and self.parent_id:
            return self.parent
        return self

    @property
    def service_consent(self):
        """``(consent_to_call, consent_to_text)`` for messaging about this order.

        INHERITED by a work order for the same reason as the phone number: the
        wizard records consent once, on the assessment, so a work order's own flags
        are always False. Without this, every text about an installation was
        blocked as "no consent" while the member had in fact consented -- which is
        what the first end-to-end reminder run showed, two messages blocked with the
        member's consent sitting on the parent order.
        """
        source = self._contact_source()
        return source.consent_to_call, source.consent_to_text


class DispatchItem(models.Model):
    """One installable thing, drawn from one housing case.

    This is the unit the business actually thinks in: "a grab bar in Brooklyn,
    approved". It comes from a Home Remediation or Home Accessibility case, whose
    program name encodes ``<family> - <item> - <borough>``.

    An item is NOT a work order. A work order is a batch an agent assembles from
    approved items, so ``dispatch_order`` is nullable and an item sits unassigned
    until someone includes it. That nullable FK is the whole "has this been
    dispatched yet?" answer -- tracking it separately would immediately drift.

    One item per case (unique), so re-importing a case cannot duplicate it.
    """

    dispatch_item_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    # The assessment that found the need. Items always hang off an assessment,
    # even before any work order exists.
    assessment = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="items",
    )
    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name="dispatch_items",
    )
    # The work order that includes this item, once one does. NULL = not yet
    # dispatched.
    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="line_items",
    )
    # Captured at creation from the program name, NOT derived on read: this is the
    # instruction a vendor acts on, and a later program rename must not rewrite
    # what was installed.
    item = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=120, blank=True)
    # The program name as it read when the item was created, for provenance.
    program_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["location", "item"]
        constraints = [
            models.UniqueConstraint(fields=["case"], name="one_item_per_case"),
        ]
        indexes = [
            models.Index(fields=["assessment", "dispatch_order"]),
        ]

    def __str__(self):
        return f"{self.item} ({self.location})" if self.item else str(self.case_id)

    @property
    def authorization_status(self):
        """Read LIVE from the case, unlike ``item``.

        Deliberately not captured: an authorization can be approved, denied or
        expire after the item exists, and a stale copy would have a vendor fitting
        something that is no longer covered.
        """
        return (self.case.service_authorization_status or "") if self.case_id else ""

    @property
    def is_approved(self):
        return self.authorization_status.lower() == "approved"

    @property
    def authorization_window(self):
        """``(start, end)`` of the case's authorization window, or ``(None, None)``.

        Delegates to ``Case.effective_authorization_window()`` rather than reading
        the fields directly, so it inherits the request-window fallback: some Unite
        Us exports carry only the REQUEST window on an already-approved
        authorization, and reading ``approval_ends_at`` alone would call those
        expired.
        """
        if not self.case_id:
            return None, None
        return self.case.effective_authorization_window()

    @property
    def authorization_expired(self):
        """True when the window has an END that is in the past.

        A missing end date is NOT expired: an approved case with no window exported
        is a gap in the source data, and refusing to install a device because Unite
        Us omitted a date would be the wrong failure.
        """
        _start, end = self.authorization_window
        return bool(end) and end < timezone.now()

    @property
    def is_available(self):
        """Selectable for a NEW work order.

        Three conditions:

        * the authorization is APPROVED -- nothing unapproved may be fitted;
        * its window has not EXPIRED -- an approval that has lapsed still reads
          "approved" on the case, which is exactly the trap the food side names
          "Authorization Expired". Installing against a lapsed authorization is
          unbilled work.
        * the item is not already in a work order -- no double-dispatch, and that
          FK is the only place that is tracked.

        The case's own STATUS is deliberately not a condition: a Home Remediation
        case can be closed in Unite Us while the approved device still has to be
        installed.
        """
        return (
            self.is_approved
            and not self.authorization_expired
            and self.dispatch_order_id is None
        )

    @property
    def unavailable_reason(self):
        """WHY this item cannot be dispatched, or "" when it can.

        ⚠ THE UI USED TO GUESS. The Work Orders panel counted "approved but not
        available" and captioned the number "already in a work order" -- a single
        cause for three different ones. MIRIAM ISRAEL had 9 approved items, NONE
        dispatched, all with authorizations that lapsed on 2026-09-18, and the panel
        said they were already in a work order. The information was here; the caption
        invented an explanation for it.

        One property, so the serializer, the error message raised by
        create_work_order and the panel all say the same thing.
        """
        if self.dispatch_order_id:
            return "already in a work order"
        if not self.is_approved:
            return self.authorization_status or "no authorization"
        if self.authorization_expired:
            _start, end = self.authorization_window
            return f"authorization expired {end:%b %-d, %Y}" if end else "not authorized"
        return ""

    @property
    def expired_only(self):
        """Approved and undispatched, blocked SOLELY by a lapsed window.

        Separated because it is the one unavailable state an agent may legitimately
        override: the work still has to happen, and Unite Us routinely leaves a case
        open past its authorization window. Unapproved or already-dispatched items are
        never overridable -- the first is unfunded and the second is double-dispatch.
        """
        return (
            self.is_approved
            and self.dispatch_order_id is None
            and self.authorization_expired
        )


class DispatchAvailabilityWindow(models.Model):
    """A window the MEMBER offered. Not the appointment.

    The wizard requires at least THREE DISTINCT DATES, each with one or more time
    windows -- so validation counts dates, not rows: three windows on one Tuesday
    does not satisfy it. The vendor then picks from these and the agreed slot
    becomes a :class:`DispatchVisit`.
    """

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="availability_windows"
    )
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "start_time"]

    def __str__(self):
        return f"{self.date} {self.start_time}-{self.end_time}"


class DispatchVisit(models.Model):
    """The agreed appointment, and the visit itself.

    Separate from the availability windows: those are what the member OFFERED,
    this is what was agreed. Confirming one is what fires the vendor's calendar
    event and reminder (vendor portal phase).
    """

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="visits"
    )
    scheduled_for = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # What was pushed to the vendor's calendar, for later reconciliation.
    calendar_event_ref = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_for"]

    def __str__(self):
        return f"Visit {self.scheduled_for:%Y-%m-%d %H:%M}" if self.scheduled_for else "Visit (unscheduled)"


class DispatchFinding(models.Model):
    """One result of the inspection. May justify a Home Remediation case.

    The submission GATE requires at least one photo PER FINDING, so a finding is
    the thing proofs attach to -- not just the order.
    """

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="findings"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # What the finding suggests should be done, free text for now; the screener
    # turns these into Home Remediation cases in Unite Us.
    recommendation = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.title


class DispatchProof(models.Model):
    """A proof image. Mirrors :class:`DeliveryOrderProof`: our S3 + a sha256.

    ``content_hash`` makes re-uploads idempotent and de-dupes the same image --
    which matters more here than for deliveries, because an OFFLINE vendor retries.

    ``finding`` is nullable: general site photos have none, and the submission gate
    counts only the photos that DO have one.
    """

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="proofs"
    )
    finding = models.ForeignKey(
        DispatchFinding, on_delete=models.CASCADE, null=True, blank=True,
        related_name="proofs",
    )
    s3_key = models.CharField(max_length=500)
    file_url = models.URLField(max_length=1000, blank=True)
    content_hash = models.CharField(max_length=64, db_index=True)
    # Which identified problem this photo evidences: an intervention GROUP code
    # ("grab_bars", "air_filtration"). Blank means a general photo of the dwelling.
    #
    # The group, not the question. Three separate questions point at grab bars, and
    # a photo per question would ask for the same photo three times; the problem
    # being evidenced is "no grab bars", however many questions surfaced it.
    #
    # A plain code rather than a FK: it names a row in the form TEMPLATE, which is
    # versioned data rather than a table, and a submitted form keeps its own frozen
    # copy of that template.
    intervention_group = models.CharField(max_length=40, blank=True, db_index=True)
    caption = models.CharField(max_length=255, blank=True)
    # Device time vs server time. The vendor signs at 14:02 in a basement and syncs
    # at 18:30; the device clock is UNTRUSTED, so both are kept.
    captured_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dispatch_order", "content_hash"],
                name="one_proof_per_hash_per_order",
            ),
        ]

    def __str__(self):
        return f"Proof {self.content_hash[:12]}"


class DispatchDocument(models.Model):
    """A document attached to an order -- signed paperwork, reports, permits."""

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="documents"
    )
    s3_key = models.CharField(max_length=500)
    file_url = models.URLField(max_length=1000, blank=True)
    content_hash = models.CharField(max_length=64, db_index=True)
    filename = models.CharField(max_length=255, blank=True)
    doc_type = models.CharField(max_length=60, blank=True)
    uploaded_by_agent = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_documents",
    )
    uploaded_by_vendor_user = models.ForeignKey(
        VendorUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.filename or f"Document {self.content_hash[:12]}"


class BillableItem(models.Model):
    """The price list: what a vendor charges us, and what we bill Unite Us.

    Transcribed from Simplified_Billable_Items_Pricing. Every one of the 26
    physical items matches an intervention option in
    ``api/services/assessment_forms.py`` EXACTLY, in both directions -- the pricing
    sheet and the assessment form are the same catalogue seen from two angles. So
    ``option_code`` is a real join rather than a label match: it is what lets a
    recommended intervention become a priced line.

    ``billing_category`` is the housing programme item ("Grab Bars"), which is also
    the intervention GROUP on the form. ``main_category`` is the sheet's own
    grouping (Bathroom, Air Quality...) and exists for display order only.

    THE BILLED PRICE IS NOT STORED. It is ``vendor_price`` plus the admin fee, and
    the fee is adjustable -- so storing the total would leave stale numbers behind
    the moment anyone changed it. An INVOICE will need to snapshot both, because a
    bill already sent must not move; that belongs with the invoicing work.
    """

    billable_item_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    # "Shower chair". The form's option label.
    item = models.CharField(max_length=120)
    # The intervention option this prices, e.g. "shower_chair". Blank only for a
    # priced line that is not an installable item -- the dwelling assessment
    # itself.
    option_code = models.CharField(max_length=60, blank=True, db_index=True)
    # "Grab Bars" -- the housing programme item, and the form's group label.
    billing_category = models.CharField(max_length=120, db_index=True)
    # The sheet's own section: Bathroom / Air Quality / Temperature Control...
    main_category = models.CharField(max_length=120, blank=True)

    # HCPCS code and modifiers, from the 1115 waiver's billing rules. Every row is
    # S5165 today; kept per row because that is a billing fact, not a constant we
    # should bake in.
    hcpcs_code = models.CharField(max_length=20, blank=True)
    modifiers = models.CharField(max_length=40, blank=True)

    # What we RECOMMEND the vendor charge us. Per-vendor pricing comes later; this
    # is the base every vendor starts from.
    vendor_price = models.DecimalField(max_digits=10, decimal_places=2)

    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "billing_category", "item"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "billing_category"],
                name="uniq_billable_item_per_category",
            ),
        ]
        indexes = [models.Index(fields=["is_active", "billing_category"])]

    def __str__(self):
        return f"{self.item} ({self.billing_category}) ${self.vendor_price}"

    def admin_fee(self, percent=None):
        """The mark-up, rounded to the cent."""
        from decimal import ROUND_HALF_UP, Decimal

        pct = BillingSettings.get().admin_fee_percent if percent is None else percent
        raw = self.vendor_price * (Decimal(pct) / Decimal("100"))
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def billed_price(self, percent=None):
        """What we bill Unite Us: the vendor price plus the admin fee."""
        return self.vendor_price + self.admin_fee(percent)


class BillingSettings(models.Model):
    """Singleton settings for housing billing.

    A row rather than a Django setting, because the operator has to be able to
    change the admin fee from the CRM without a deploy -- which is the whole point
    of "make the 10% adjustable".
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    # The mark-up on a vendor's price, as a percentage. 10% today.
    admin_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00"),
    )
    # The most a vendor may recommend on one assessment, INCLUDING the assessment's
    # own fee. Unite Us authorises up to $10,000 for this service and our admin fee
    # comes out of that, leaving $9,000 of vendor spend.
    #
    # Stored rather than derived from admin_fee_percent, because the two are not the
    # same arithmetic: 10% treated as a SHARE of the $10,000 gives $9,000 (billing
    # $9,900), while 10% as a MARKUP on the vendor price would allow $9,090.91
    # (billing exactly $10,000). $9,000 is the figure the business states and it is
    # the conservative one; deriving it would quietly change the cap whenever
    # somebody edited the fee.
    vendor_spend_cap = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("9000.00"),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="billing_settings_updates",
    )

    class Meta:
        verbose_name_plural = "Billing settings"

    def __str__(self):
        return f"Admin fee {self.admin_fee_percent}%"

    def save(self, *args, **kwargs):
        # One row, always. A second would make "what is the fee?" ambiguous.
        self.singleton_id = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(singleton_id=1)
        return obj


class VendorPrice(models.Model):
    """One vendor's price for one billable item -- an OVERRIDE, not a copy.

    Absent means "use the base price". That is deliberately not the same as copying
    all 27 rows to every vendor:

    * a new item added to the base list is immediately available to every vendor,
      rather than needing 27 inserts per vendor and being silently missing until
      someone remembers;
    * changing a base price updates every vendor who has not negotiated their own;
    * "which prices did we actually agree with this vendor?" is answerable by
      looking at which rows exist.

    THE INVOICE MUST SNAPSHOT THIS. A bill already sent must not move when a price
    is renegotiated, so an invoice line will store the price and fee it used rather
    than pointing here. This table is what the NEXT invoice will be built from.
    """

    vendor_price_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    vendor = models.ForeignKey(
        "Vendor", on_delete=models.CASCADE, related_name="prices",
    )
    billable_item = models.ForeignKey(
        BillableItem, on_delete=models.CASCADE, related_name="vendor_prices",
    )
    # What THIS vendor charges us for this item.
    price = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vendor_price_updates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["billable_item__sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "billable_item"],
                name="uniq_vendor_price_per_item",
            ),
        ]

    def __str__(self):
        return f"{self.vendor_id} {self.billable_item_id} ${self.price}"


class MessageDirection(models.TextChoices):
    OUTBOUND = "outbound", "Outbound"
    INBOUND = "inbound", "Inbound"


class MessageStatus(models.TextChoices):
    # Outbound lifecycle. QUEUED means stored but not yet handed to a provider,
    # which is every message until Twilio is wired up.
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    # Never handed to a provider at all, and why. Kept as a real status rather
    # than a silent no-op: "we did not text the member" is something an agent has
    # to be able to SEE, not infer from an absence.
    BLOCKED = "blocked", "Blocked"
    # Inbound.
    RECEIVED = "received", "Received"


class MessageKind(models.TextChoices):
    APPOINTMENT_SCHEDULED = "appointment_scheduled", "Appointment scheduled"
    APPOINTMENT_REMINDER = "appointment_reminder", "Appointment reminder"
    APPOINTMENT_CHANGED = "appointment_changed", "Appointment changed"
    FREEFORM = "freeform", "Free text"
    INBOUND_REPLY = "inbound_reply", "Inbound reply"


class MemberMessage(models.Model):
    """Every SMS to or from a member. One row per message, both directions.

    Stored BEFORE any provider is called, and stored even when we decline to send,
    because the requirement is a complete record of communication with the member
    -- and a log that only contains successes cannot answer "did anyone tell her?".

    Twilio is not wired up yet (see ``api/services/messaging.py``). Until it is,
    outbound rows are created with status QUEUED and no provider id, which is
    exactly what they will look like in the instant before a real send anyway.
    """

    message_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    # Nullable: an inbound text can arrive from a number we cannot match to a
    # member, and dropping it would lose the one thing that might identify them.
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="messages",
        null=True, blank=True,
    )
    direction = models.CharField(max_length=10, choices=MessageDirection.choices)
    kind = models.CharField(
        max_length=32, choices=MessageKind.choices, default=MessageKind.FREEFORM,
    )
    status = models.CharField(
        max_length=12, choices=MessageStatus.choices,
        default=MessageStatus.QUEUED, db_index=True,
    )

    to_number = models.CharField(max_length=32, blank=True)
    from_number = models.CharField(max_length=32, blank=True)
    body = models.TextField()

    # What this message was ABOUT, so a member's log reads as a story rather than a
    # pile of texts.
    dispatch_order = models.ForeignKey(
        "DispatchOrder", on_delete=models.SET_NULL, related_name="messages",
        null=True, blank=True,
    )

    provider = models.CharField(max_length=20, blank=True)
    provider_message_id = models.CharField(max_length=64, blank=True, db_index=True)
    error_code = models.CharField(max_length=40, blank=True)
    error_detail = models.TextField(blank=True)

    # Who caused it. Both null = the system did it (a reminder). SET_NULL on both,
    # because a message is history and must outlive the account that sent it.
    sent_by_vendor_user = models.ForeignKey(
        "VendorUser", on_delete=models.SET_NULL, related_name="messages_sent",
        null=True, blank=True,
    )
    sent_by_agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, related_name="member_messages_sent",
        null=True, blank=True,
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "-created_at"]),
            models.Index(fields=["direction", "status"]),
        ]

    def __str__(self):
        who = self.client_id or self.from_number or "unknown"
        return f"{self.direction} {self.kind} {who}"


class ReminderKind(models.TextChoices):
    DAY_BEFORE = "day_before", "Day before"
    THIRTY_MIN = "thirty_min", "30 minutes before"


class ReminderAudience(models.TextChoices):
    VENDOR = "vendor", "Vendor"
    MEMBER = "member", "Member"


class DispatchReminder(models.Model):
    """A reminder to fire for a scheduled visit.

    Rows in a table rather than a per-vendor calendar integration: the requirement
    is "remind them the day before and 30 minutes before", and a row with a
    ``send_at`` is something we can inspect, re-run and prove. A Google/Outlook
    calendar would move that state somewhere we cannot query when a vendor says
    they were never told.

    ``calendar_event_ref`` on DispatchVisit stays available for a real calendar
    integration later; these reminders do not depend on it.
    """

    reminder_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    visit = models.ForeignKey(
        DispatchVisit, on_delete=models.CASCADE, related_name="reminders",
    )
    kind = models.CharField(max_length=16, choices=ReminderKind.choices)
    audience = models.CharField(
        max_length=10, choices=ReminderAudience.choices,
        default=ReminderAudience.VENDOR,
    )
    send_at = models.DateTimeField(db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # A reschedule cancels the unsent reminders for the old time rather than
    # editing them, so the record shows that a reminder for the old slot existed
    # and was stood down.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["send_at"]
        indexes = [models.Index(fields=["send_at", "sent_at"])]

    def __str__(self):
        return f"{self.audience} {self.kind} at {self.send_at:%Y-%m-%d %H:%M}"

    @property
    def is_pending(self):
        return self.sent_at is None and self.cancelled_at is None


class DispatchQuestionnaireState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"


class DispatchQuestionnaire(models.Model):
    """The Dwelling Assessment form for one assessment order.

    One per order. Which MODULES it contains follows the order's referral type --
    Mobility, Ventilation, or both for Combined -- so the form a vendor sees is
    bounded by what was authorized.

    Answers are stored as JSON keyed by the question codes in
    ``api/services/assessment_forms.py`` rather than as columns. The questions are
    CONTENT: adding one should not need a migration, and 33 boolean columns would
    churn the schema at every revision of the form.

    WRITABLE BY THE VENDOR ONLY. There is deliberately no CRM endpoint that writes
    these answers -- the CRM renders them read-only. That is the locking rule the
    whole feature rests on, enforced by the absence of a route rather than by a
    permission check someone can widen later.
    """

    dispatch_questionnaire_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    dispatch_order = models.OneToOneField(
        DispatchOrder, on_delete=models.CASCADE, related_name="questionnaire",
    )
    # ["mobility"], ["ventilation"], or both.
    modules = models.JSONField(default=list, blank=True)
    template_version = models.PositiveIntegerField(default=1)
    # The form definition as it stood when SUBMITTED. Empty while a draft: a draft
    # renders against the live template, but a signed form must render years later
    # exactly as it was signed rather than acquiring blank questions from a later
    # revision.
    schema_snapshot = models.JSONField(default=dict, blank=True)

    # {"mob.reason.fall_risk": true, ...}
    answers = models.JSONField(default=dict, blank=True)
    # The "Other..." box each Reason for Assessment section carries:
    # {"mob.reason": "..."}
    section_other = models.JSONField(default=dict, blank=True)
    # [{"option": "window_ac", "qty": 2}, ...] -- QUANTIFIED, not merely ticked:
    # the form has a -/+ stepper per option and a member can need two air
    # conditioners.
    interventions = models.JSONField(default=list, blank=True)

    justification = models.TextField(blank=True)   # Clinical / Safety Justification
    assessor_notes = models.TextField(blank=True)

    state = models.CharField(
        max_length=10, choices=DispatchQuestionnaireState.choices,
        default=DispatchQuestionnaireState.DRAFT, db_index=True,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Assessment form ({self.state}) for {self.dispatch_order_id}"

    @property
    def is_submitted(self):
        return self.state == DispatchQuestionnaireState.SUBMITTED

    def schema(self):
        """The definition to render against.

        The frozen snapshot once submitted, the live template while a draft.
        """
        from api.services.assessment_forms import build_schema

        if self.is_submitted and self.schema_snapshot:
            return self.schema_snapshot
        return build_schema(self.modules or [])


class DispatchSubmissionState(models.TextChoices):
    ACTIVE = "active", "Active"
    VOIDED = "voided", "Voided"


class DispatchSubmission(models.Model):
    """One submission of an order's evidence. APPEND-ONLY, one ACTIVE at a time.

    The submission -- not the order -- is the versioned thing, which is how the two
    rules coexist: answers cannot change after submission (they are locked), yet a
    vendor may correct a mistake. A correction VOIDS this submission and creates
    the next one; the voided copy, its signatures and its PDF survive untouched.

    ``pdf_sha256`` is what makes the voided copy worth keeping: it proves the old
    record was not quietly edited to match the new story.

    Note the asymmetry -- the VENDOR may void their own submission; CRM users may
    not edit anything, ever. The correction belongs to the party who signed.
    """

    dispatch_submission_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="submissions"
    )
    sequence = models.PositiveIntegerField(default=1)
    state = models.CharField(
        max_length=10, choices=DispatchSubmissionState.choices,
        default=DispatchSubmissionState.ACTIVE, db_index=True,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        VendorUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_submissions",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        VendorUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_submissions_voided",
    )
    void_reason = models.TextField(blank=True)
    # The immutable snapshot of what was signed.
    pdf_s3_key = models.CharField(max_length=500, blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["dispatch_order", "sequence"],
                name="one_submission_per_sequence",
            ),
            # Exactly one live submission per order; voided ones are unconstrained.
            models.UniqueConstraint(
                fields=["dispatch_order"], condition=models.Q(state="active"),
                name="one_active_submission_per_order",
            ),
        ]

    def __str__(self):
        return f"Submission #{self.sequence} ({self.state})"


class DispatchSignerRole(models.TextChoices):
    VENDOR = "vendor", "Vendor"
    MEMBER = "member", "Member"


class DispatchSignature(models.Model):
    """A signature on a SUBMISSION -- not on the order.

    Tied to the submission so a void keeps the signatures that belonged to it:
    submission #1 still shows exactly who signed what, even after #2 supersedes it.

    The gate needs BOTH roles present before a submission can be made.
    """

    dispatch_submission = models.ForeignKey(
        DispatchSubmission, on_delete=models.CASCADE, related_name="signatures"
    )
    signer_role = models.CharField(max_length=10, choices=DispatchSignerRole.choices)
    signer_name = models.CharField(max_length=255, blank=True)
    s3_key = models.CharField(max_length=500, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["signed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dispatch_submission", "signer_role"],
                name="one_signature_per_role_per_submission",
            ),
        ]

    def __str__(self):
        return f"{self.get_signer_role_display()} signature"


class DispatchUniteUsUpload(models.Model):
    """A record that a human uploaded this order's evidence to Unite Us.

    The CRM pushes NOTHING. This is the record of the manual action, so "what is
    still waiting to go to Unite Us?" is answerable instead of living in someone's
    memory -- a queue, not just a log.

    ``uploaded_at`` and ``recorded_at`` are separate on purpose: an agent uploads at
    10:00 and ticks the box at 16:30. One field standing for two events is exactly
    the confusion that ``Case.case_created_at`` (source time, not ingestion) caused.
    """

    dispatch_order = models.ForeignKey(
        DispatchOrder, on_delete=models.CASCADE, related_name="uniteus_uploads"
    )
    # Which submission's evidence went up -- so a later VOID can mark this
    # superseded rather than leaving Unite Us holding a document we now reject.
    dispatch_submission = models.ForeignKey(
        DispatchSubmission, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="uniteus_uploads",
    )
    uploaded_by = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatch_uniteus_uploads",
    )
    uploaded_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    uniteus_ref = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True)
    # Set when the submission it covered was voided: Unite Us holds evidence the
    # CRM no longer considers current, and someone must re-upload.
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Uploaded {self.uploaded_at:%Y-%m-%d}"
