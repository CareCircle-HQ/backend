from django.contrib import admin

from .models import (
    Address,
    Agent,
    Assessment,
    Case,
    Client,
    IdentifiedSocialNeed,
    Insurance,
    MilitaryProfile,
    Program,
    ProgramMainCategory,
    ProgramPipeline,
    Provider,
    Screening,
    Service,
    SocialCareCoverage,
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


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "client_id",
        "first_name",
        "last_name",
        "client_email_address",
        "consent_accepted",
        "attestation_needed",
        "crm_contact_id",
    )
    list_filter = (
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
        "provider",
        "date_opened",
    )
    list_filter = ("case_status", "service_authorization_status", "case_is_referred")
    search_fields = (
        "case_id",
        "client__last_name",
        "provider_name",
        "created_by_name",
    )
    autocomplete_fields = ("client", "provider", "originating_provider", "program", "previous_case")


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
