from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Address,
    Agent,
    AgentLoginCode,
    AllowedZipCode,
    Assessment,
    Case,
    Client,
    EnrollmentVerification,
    Household,
    HouseholdMember,
    IdentifiedSocialNeed,
    ImportRun,
    Insurance,
    MemberVerification,
    MilitaryProfile,
    Note,
    Program,
    ProgramMainCategory,
    ProgramPipeline,
    Provider,
    Screening,
    Service,
    SocialCareCoverage,
    Ticket,
    TimelineEvent,
    UniteUsCredential,
    VerifiedSocialNeed,
)


class MilitaryProfileInline(admin.StackedInline):
    model = MilitaryProfile
    extra = 0


class AddressInline(admin.TabularInline):
    model = Address
    extra = 0


class InsuranceInline(admin.TabularInline):
    model = Insurance
    extra = 0


class SocialCareCoverageInline(admin.TabularInline):
    model = SocialCareCoverage
    extra = 0


class MemberVerificationInline(admin.TabularInline):
    model = MemberVerification
    extra = 0
    autocomplete_fields = ("client",)


@admin.register(EnrollmentVerification)
class EnrollmentVerificationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "client",
        "household",
        "program_name",
        "stage",
        "is_family_verified",
        "medicaid_type_verified",
        "delivery_address_verified",
        "renewal_number",
        "opened_at",
    )
    list_filter = (
        "stage",
        "is_family_verified",
        "medicaid_type_verified",
        "delivery_address_verified",
    )
    search_fields = (
        "client__client_id",
        "client__first_name",
        "client__last_name",
        "program_name",
        "code",
    )
    autocomplete_fields = ("client", "household")
    raw_id_fields = ("case", "delivery_address")
    readonly_fields = ("opened_at", "stage_at", "closed_at")
    inlines = (MemberVerificationInline,)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "client_id",
        "first_name",
        "last_name",
        "client_email_address",
        "lifecycle_stage",
        "is_level",
        "consent_accepted",
        "attestation_needed",
        "crm_contact_id",
    )
    list_filter = (
        "lifecycle_stage",
        "is_level",
        "consent_accepted",
        "attestation_needed",
        "is_a_family",
        "call_transfer_answered",
    )
    search_fields = (
        "client_id",
        "first_name",
        "last_name",
        "client_email_address",
        "client_phone_number",
        "crm_contact_id",
        "agent_code",
        "lead_source",
    )
    inlines = [
        MilitaryProfileInline,
        AddressInline,
        InsuranceInline,
        SocialCareCoverageInline,
    ]


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("client", "type", "city", "state")
    list_filter = ("type", "state")


@admin.register(Insurance)
class InsuranceAdmin(admin.ModelAdmin):
    list_display = ("client", "plan_name", "plan_type", "status", "is_primary")
    list_filter = ("plan_type", "status", "is_primary")


@admin.register(SocialCareCoverage)
class SocialCareCoverageAdmin(admin.ModelAdmin):
    list_display = ("client", "plan_name", "plan_type", "status", "enrolled_at", "expired_at")
    list_filter = ("plan_type", "status")
    search_fields = ("client__client_id", "plan_name", "external_member_id")


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("provider_id", "name", "network_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("provider_id", "name", "network_name")


@admin.register(ProgramMainCategory)
class ProgramMainCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = ("program_id", "name", "main_category", "provider")
    list_filter = ("main_category",)
    search_fields = ("program_id", "name")
    autocomplete_fields = ("provider", "main_category")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "program", "is_offered", "is_active")
    list_filter = ("is_offered", "is_active")
    search_fields = ("code", "name")
    autocomplete_fields = ("program",)


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = (
        "case_id",
        "client",
        "case_status",
        "service_type",
        "case_type",
        "household_type",
        "provider",
        "date_opened",
    )
    list_filter = (
        "case_type",
        "household_type",
        "case_status",
        "service_authorization_status",
        "case_is_referred",
    )
    search_fields = (
        "case_id",
        "client__last_name",
        "provider_name",
        "created_by_name",
    )
    autocomplete_fields = ("client", "provider", "originating_provider", "program", "previous_case")


class HouseholdMemberInline(admin.TabularInline):
    model = HouseholdMember
    extra = 0
    autocomplete_fields = ("client",)


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("household_id", "name", "member_count", "created_at")
    search_fields = ("household_id", "name", "members__client__client_id")
    inlines = (HouseholdMemberInline,)

    @admin.display(description="members")
    def member_count(self, obj):
        return obj.members.count()


@admin.register(HouseholdMember)
class HouseholdMemberAdmin(admin.ModelAdmin):
    list_display = ("client", "household", "is_primary", "relationship", "added_at")
    list_filter = ("is_primary",)
    search_fields = ("client__client_id", "client__last_name", "household__name")
    autocomplete_fields = ("client", "household")


class IdentifiedSocialNeedInline(admin.TabularInline):
    model = IdentifiedSocialNeed
    extra = 0


class VerifiedSocialNeedInline(admin.TabularInline):
    model = VerifiedSocialNeed
    extra = 0


@admin.register(Screening)
class ScreeningAdmin(admin.ModelAdmin):
    list_display = (
        "enhanced_screen_id",
        "client",
        "screen_status",
        "screen_type",
        "screen_created_at",
    )
    list_filter = ("screen_status", "screen_type")
    search_fields = (
        "enhanced_screen_id",
        "subject_id",
        "client__last_name",
    )
    autocomplete_fields = ("client",)


@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = (
        "assessment_id",
        "client",
        "eligible_status",
        "screen_created_at",
    )
    list_filter = ("eligible_status",)
    search_fields = (
        "assessment_id",
        "subject_id",
        "client__last_name",
    )
    autocomplete_fields = ("client",)


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = (
        "name", "agent_code", "email", "group", "status", "cbo",
        "is_manager", "calltools_synced_at",
    )
    list_filter = ("group", "status", "cbo", "is_agent", "is_manager", "is_account_owner")
    search_fields = ("name", "agent_code", "email", "username", "calltools_app_user")
    readonly_fields = ("calltools_synced_at",)
    ordering = ("name",)


@admin.register(AgentLoginCode)
class AgentLoginCodeAdmin(admin.ModelAdmin):
    # The plaintext code is never stored (only code_hash); nothing sensitive to
    # expose here. Useful for auditing 2FA requests/usage.
    list_display = (
        "email", "agent", "expires_at", "consumed_at", "attempts", "created_at",
    )
    list_filter = ("consumed_at",)
    search_fields = ("email", "agent__name", "agent__email")
    readonly_fields = ("code_hash", "created_at")
    autocomplete_fields = ("agent",)
    ordering = ("-created_at",)


@admin.register(AllowedZipCode)
class AllowedZipCodeAdmin(admin.ModelAdmin):
    list_display = ("zip_code", "borough", "scn", "platform", "is_active")
    list_filter = ("is_active", "platform", "scn")
    search_fields = ("zip_code", "borough")
    ordering = ("zip_code",)


@admin.register(ProgramPipeline)
class ProgramPipelineAdmin(admin.ModelAdmin):
    list_display = (
        "program_name",
        "case_category",
        "pipeline_name",
        "pipeline_id",
        "updated_at",
    )
    list_filter = ("case_category", "main_category", "pipeline_name")
    search_fields = ("program_name", "pipeline_id")
    ordering = ("program_name",)


@admin.register(UniteUsCredential)
class UniteUsCredentialAdmin(admin.ModelAdmin):
    # Never expose the encrypted token columns in the admin form/list.
    list_display = (
        "provider_id", "employee_id", "agent", "status",
        "access_expires_at", "last_captured_at", "last_refreshed_at",
    )
    list_filter = ("status",)
    search_fields = ("provider_id", "employee_id")
    exclude = ("access_token", "refresh_token")
    readonly_fields = ("last_captured_at", "last_refreshed_at", "created_at", "updated_at")
    autocomplete_fields = ("agent",)


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "status", "triggered_by",
        "processed_count", "created_count", "updated_count",
        "skipped_count", "error_count", "started_at", "finished_at",
    )
    list_filter = ("source", "status")
    search_fields = ("source", "triggered_by")
    readonly_fields = ("started_at",)


@admin.register(Note)
class NoteAdmin(SimpleHistoryAdmin):
    list_display = ("id", "source", "client", "case", "author_name", "source_created_at")
    list_filter = ("source",)
    search_fields = ("source_note_id", "author_name", "body")
    autocomplete_fields = ("client", "case")


@admin.register(Ticket)
class TicketAdmin(SimpleHistoryAdmin):
    list_display = (
        "id", "type", "status", "severity", "client", "case",
        "assigned_to", "created_at",
    )
    list_filter = ("status", "type", "severity")
    search_fields = ("reason",)
    autocomplete_fields = ("client", "case", "assigned_to", "import_run")


@admin.register(TimelineEvent)
class TimelineEventAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_at", "event_type", "client", "title", "badge_text",
        "badge_tone", "renewal_number", "source", "actor",
    )
    list_filter = ("event_type", "badge_tone", "source", "renewal_number")
    search_fields = (
        "client__client_id", "client__first_name", "client__last_name",
        "title", "subtitle", "actor", "dedupe_key", "object_id",
    )
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "-created_at")
    raw_id_fields = ("client", "enrollment", "content_type")
    readonly_fields = ("created_at",)
