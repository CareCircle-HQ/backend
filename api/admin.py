from django.contrib import admin

from .models import (
    Address,
    Agent,
    Answer,
    Assessment,
    AssessmentQuestionnaire,
    Case,
    Client,
    Eligibility,
    IdentifiedSocialNeed,
    ImportBatch,
    Insurance,
    MilitaryProfile,
    Program,
    ProgramPipeline,
    Provider,
    Question,
    Questionnaire,
    QuestionOption,
    ScreenTemplate,
    Screening,
    ScreeningForm,
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
    inlines = [MilitaryProfileInline, AddressInline, InsuranceInline]


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("client", "type", "city", "state")
    list_filter = ("type", "state")


@admin.register(Insurance)
class InsuranceAdmin(admin.ModelAdmin):
    list_display = ("client", "plan_name", "plan_type", "status", "is_primary")
    list_filter = ("plan_type", "status", "is_primary")


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("provider_id", "name", "network_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("provider_id", "name", "network_name")


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = ("program_id", "name", "provider")
    search_fields = ("program_id", "name")


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


class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 0
    fields = ("question", "answer_type", "answer_value", "answer_score", "answer_status")
    autocomplete_fields = ("question",)


class IdentifiedSocialNeedInline(admin.TabularInline):
    model = IdentifiedSocialNeed
    extra = 0


class VerifiedSocialNeedInline(admin.TabularInline):
    model = VerifiedSocialNeed
    extra = 0


@admin.register(ScreenTemplate)
class ScreenTemplateAdmin(admin.ModelAdmin):
    list_display = ("template_id", "template_title", "template_type", "template_status", "active_template")
    list_filter = ("active_template", "template_type")
    search_fields = ("template_id", "template_title", "template_loinc_code")


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
    inlines = [AnswerInline]


@admin.register(Eligibility)
class EligibilityAdmin(admin.ModelAdmin):
    list_display = (
        "eligibility_id",
        "client",
        "eligible_status",
        "screen_created_at",
    )
    list_filter = ("eligible_status",)
    search_fields = (
        "eligibility_id",
        "subject_id",
        "client__last_name",
    )
    autocomplete_fields = ("client",)
    inlines = [AnswerInline]


class QuestionOptionInline(admin.TabularInline):
    model = QuestionOption
    extra = 0


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("question_id", "question_primary_text", "question_type", "question_category", "question_is_active")
    list_filter = ("question_is_active", "question_type", "admin_only")
    search_fields = ("question_id", "question_primary_text", "question_loinc_code")
    autocomplete_fields = ("template", "parent_question")
    inlines = [QuestionOptionInline]


@admin.register(QuestionOption)
class QuestionOptionAdmin(admin.ModelAdmin):
    list_display = ("question_option_id", "question_option_text", "question_option_type", "question_option_is_active")
    search_fields = ("question_option_id", "question_option_text")
    autocomplete_fields = ("question", "parent_question_option")


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("answer_id", "screening", "eligibility", "question", "answer_type", "answer_status")
    list_filter = ("answer_type", "answer_status", "answer_is_active")
    search_fields = ("answer_id",)
    autocomplete_fields = ("screening", "eligibility", "question", "question_option")


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "status",
        "file_name",
        "row_count",
        "success_count",
        "error_count",
        "imported_by",
        "imported_at",
    )
    list_filter = ("source", "status")
    search_fields = ("file_name",)
    readonly_fields = ("imported_at",)


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


@admin.register(ScreeningForm)
class ScreeningFormAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(Questionnaire)
class QuestionnaireAdmin(admin.ModelAdmin):
    list_display = ("title", "screening")
    search_fields = ("title", "screening__name")
    autocomplete_fields = ("screening",)


@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(AssessmentQuestionnaire)
class AssessmentQuestionnaireAdmin(admin.ModelAdmin):
    list_display = ("title", "assessment")
    search_fields = ("title", "assessment__name")
    autocomplete_fields = ("assessment",)
