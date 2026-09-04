from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Address,
    Agent,
    AgentLoginCode,
    AllowedState,
    AllowedZipCode,
    Assessment,
    ServiceZipCode,
    CadenceRule,
    Case,
    Client,
    EnrollmentVerification,
    Household,
    HouseholdMember,
    HouseholdMemberLoginCode,
    IdentifiedSocialNeed,
    ImportRun,
    Insurance,
    Kitchen,
    KitchenMenuType,
    Lead,
    MemberDeliverySchedule,
    MemberDietaryProfile,
    MenuType,
    MilitaryProfile,
    Note,
    ActiveProgram,
    ProductType,
    Program,
    ProgramMainCategory,
    Provider,
    Screening,
    Service,
    SocialCareCoverage,
    Ticket,
    TicketType,
    TimelineEvent,
    UniteUsAgent,
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


class MemberDietaryProfileInline(admin.TabularInline):
    model = MemberDietaryProfile
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
    inlines = (MemberDietaryProfileInline,)


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
    list_display = ("client", "type", "street", "unit", "city", "state", "zip")
    list_filter = ("type", "state")
    search_fields = (
        "client__client_id",
        "client__first_name",
        "client__last_name",
        "street",
        "unit",
        "city",
        "zip",
    )
    list_select_related = ("client",)
    ordering = ("-id",)


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


@admin.register(ProductType)
class ProductTypeAdmin(admin.ModelAdmin):
    list_display = (
        "type",
        "prod_per_delivery",
        "delivery_days_cadence",
        "created_at",
        "updated_at",
    )
    list_filter = ("type", "delivery_days_cadence")
    search_fields = ("type", "delivery_days_cadence")
    readonly_fields = ("product_type_id", "created_at", "updated_at")
    ordering = ("type",)


@admin.register(CadenceRule)
class CadenceRuleAdmin(admin.ModelAdmin):
    list_display = (
        "product_kind",
        "accepted_weekday",
        "cadence",
        "delivery_weekdays",
        "po_weekdays",
        "first_delivery_weekday",
        "is_active",
    )
    list_filter = ("product_kind", "cadence", "is_active")
    ordering = ("product_kind", "accepted_weekday")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MemberDietaryProfile)
class MemberDietaryProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "member_name",
        "client",
        "enrollment",
        "status",
        "meal_category",
        "menu_type",
        "kitchen_meal_type",
        "meals_per_delivery",
        "updated_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "meal_category", "menu_type")
    search_fields = (
        "member_name",
        "client__client_id",
        "client__first_name",
        "client__last_name",
        "enrollment__code",
        "enrollment__client__client_id",
    )
    autocomplete_fields = ("client", "enrollment")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(MemberDeliverySchedule)
class MemberDeliveryScheduleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "member_name",
        "enrollment",
        "program",
        "product_type",
        "delivery_days_cadence",
        "prod_per_delivery",
        "meals_boxes_total",
        "menu_type",
        "status",
        "starts_on",
        "ends_on",
        "created_at",
    )
    list_filter = ("status", "delivery_days_cadence", "menu_type", "product_type")
    search_fields = (
        "member_name",
        "enrollment__code",
        "enrollment__client__client_id",
        "program__name",
    )
    autocomplete_fields = (
        "enrollment",
        "household_member",
        "program",
        "product_type",
        "member_profile",
    )
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)


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
    list_display = (
        "client", "household", "is_primary", "relationship",
        "mobile_app_username", "added_at",
    )
    list_filter = ("is_primary",)
    search_fields = (
        "client__client_id", "client__last_name", "household__name",
        "mobile_app_username",
    )
    autocomplete_fields = ("client", "household")


@admin.register(HouseholdMemberLoginCode)
class HouseholdMemberLoginCodeAdmin(admin.ModelAdmin):
    # Plaintext codes are never stored (only code_hash). Useful for auditing
    # member-app 2FA requests/usage.
    list_display = (
        "mobile_number", "member", "created_at", "expires_at",
        "consumed_at", "attempts",
    )
    search_fields = ("mobile_number",)
    readonly_fields = ("code_hash", "created_at")
    ordering = ("-created_at",)


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
        "name", "agent_code", "email", "group", "is_manager", "status", "cbo",
        "calltools_synced_at",
    )
    list_editable = ("group", "is_manager", "status")
    list_filter = ("group", "status", "cbo", "is_agent", "is_manager", "is_account_owner")
    search_fields = ("name", "agent_code", "email", "username", "calltools_app_user")
    readonly_fields = ("calltools_synced_at",)
    ordering = ("name",)


@admin.register(UniteUsAgent)
class UniteUsAgentAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "work_title", "status", "user_id", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "first_name", "last_name", "email", "user_id", "employee_id")
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


@admin.register(AllowedState)
class AllowedStateAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(ServiceZipCode)
class ServiceZipCodeAdmin(admin.ModelAdmin):
    list_display = ("zip", "borough", "is_active", "updated_at")
    list_filter = ("borough", "is_active")
    search_fields = ("zip", "borough")
    ordering = ("zip",)


@admin.register(ActiveProgram)
class ActiveProgramAdmin(admin.ModelAdmin):
    list_display = (
        "program_name",
        "case_category",
        "case_type",
        "is_for_household",
        "main_category",
        "updated_at",
    )
    list_filter = ("case_category", "case_type", "is_for_household", "main_category")
    search_fields = ("program_name", "case_category")
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


@admin.register(TicketType)
class TicketTypeAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "default_severity", "is_active", "updated_at")
    list_filter = ("is_active", "default_severity")
    search_fields = ("code", "label", "description")
    readonly_fields = ("ticket_type_id", "created_at", "updated_at")
    ordering = ("label",)


@admin.register(Ticket)
class TicketAdmin(SimpleHistoryAdmin):
    list_display = (
        "id", "type", "status", "severity", "client", "case",
        "assigned_to", "created_at",
    )
    list_filter = ("status", "type", "severity", ("created_at", admin.DateFieldListFilter))
    # Drill-down bar (year -> month -> day) at the top of the changelist: click a
    # specific date to show only that day's tickets, then use the header
    # "select all" checkbox / "Select all N tickets" link to bulk-select them.
    date_hierarchy = "created_at"
    list_select_related = ("type", "client", "case", "assigned_to")
    search_fields = ("reason",)
    autocomplete_fields = ("type", "client", "case", "assigned_to", "import_run")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "lead_id", "first_name", "last_name", "phone_number", "email",
        "zip_code", "medicaid_enrollment", "status", "assigned_to",
        "converted_client", "do_not_contact", "created_at",
    )
    list_filter = (
        "status", "medicaid_enrollment", "do_not_contact",
        "disclaimer_accepted", "preferred_contact_method",
    )
    search_fields = ("first_name", "last_name", "phone_number", "email", "zip_code")
    autocomplete_fields = ("assigned_to", "converted_client")
    filter_horizontal = ("interested_programs",)
    readonly_fields = ("lead_id", "disclaimer_accepted_at", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(TimelineEvent)
class TimelineEventAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_at", "event_type", "client", "title", "badge_text",
        "badge_tone", "renewal_number", "source", "actor",
    )
    list_filter = (
        "event_type", "badge_tone", "source", "renewal_number",
        # Filter by INSERTION date (when a row was written) as well as the
        # drill-down on occurred_at -- lets you isolate the rows a given import
        # created (e.g. to bulk-delete a bad import's timeline events).
        ("created_at", admin.DateFieldListFilter),
        ("occurred_at", admin.DateFieldListFilter),
    )
    search_fields = (
        "client__client_id", "client__first_name", "client__last_name",
        "title", "subtitle", "actor", "dedupe_key", "object_id",
    )
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "-created_at")
    raw_id_fields = ("client", "enrollment", "content_type")
    readonly_fields = ("created_at",)


@admin.register(MenuType)
class MenuTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("name",)


class KitchenMenuTypeInline(admin.TabularInline):
    model = KitchenMenuType
    extra = 0
    autocomplete_fields = ("menu_type",)


@admin.register(Kitchen)
class KitchenAdmin(admin.ModelAdmin):
    list_display = (
        "name", "status", "supported_products", "max_orders_per_day",
        "email", "updated_at",
    )
    list_filter = ("status",)
    search_fields = ("name", "email", "address")
    ordering = ("name",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (KitchenMenuTypeInline,)
