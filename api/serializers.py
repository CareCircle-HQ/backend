from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from .models import (
    WEEKDAYS,
    Address,
    Answer,
    Case,
    Client,
    CommunicationTimeOfDay,
    Eligibility,
    IdentifiedSocialNeed,
    ImportBatch,
    Insurance,
    MilitaryProfile,
    Program,
    Provider,
    Question,
    QuestionOption,
    ScreenTemplate,
    Screening,
    ServiceType,
    VerifiedSocialNeed,
)

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "first_name", "last_name")

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_staff")


# ===========================================================================
# Helpers
# ===========================================================================
def _resolve_provider(provider_id, name=None, network_id=None, network_name=None):
    if not provider_id:
        return None
    defaults = {}
    if name is not None:
        defaults["name"] = name or ""
    if network_id is not None:
        defaults["network_id"] = network_id
    if network_name is not None:
        defaults["network_name"] = network_name or ""
    obj, _ = Provider.objects.update_or_create(
        provider_id=provider_id, defaults=defaults
    )
    return obj


def _resolve_program(program_id, name=None, provider=None):
    if not program_id:
        return None
    defaults = {}
    if name is not None:
        defaults["name"] = name or ""
    if provider is not None:
        defaults["provider"] = provider
    obj, _ = Program.objects.update_or_create(
        program_id=program_id, defaults=defaults
    )
    return obj


# ===========================================================================
# Provider / Program
# ===========================================================================
class ProviderSerializer(serializers.ModelSerializer):
    provider_id = serializers.UUIDField()

    class Meta:
        model = Provider
        fields = "__all__"

    def create(self, validated_data):
        pid = validated_data.pop("provider_id")
        obj, _ = Provider.objects.update_or_create(provider_id=pid, defaults=validated_data)
        return obj


class ProgramSerializer(serializers.ModelSerializer):
    program_id = serializers.UUIDField()

    class Meta:
        model = Program
        fields = "__all__"


class ImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportBatch
        fields = "__all__"
        read_only_fields = ("imported_at", "imported_by")


# ===========================================================================
# Client domain
# ===========================================================================
class MilitaryProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilitaryProfile
        exclude = ("id", "client")


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        exclude = ("id", "client")


class InsuranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Insurance
        exclude = ("id", "client")


class ClientSerializer(serializers.ModelSerializer):
    client_id = serializers.UUIDField()
    military_profile = MilitaryProfileSerializer(required=False, allow_null=True)
    addresses = AddressSerializer(many=True, required=False)
    insurances = InsuranceSerializer(many=True, required=False)

    class Meta:
        model = Client
        fields = "__all__"

    def _validate_services(self, value, field_name):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                f"{field_name} must be a list of service codes."
            )
        valid = set(ServiceType.values)
        invalid = [v for v in value if v not in valid]
        if invalid:
            raise serializers.ValidationError(
                f"Invalid {field_name} values: {invalid}. "
                f"Allowed: {sorted(valid)}"
            )
        return value

    def validate_eligible_for(self, value):
        return self._validate_services(value, "eligible_for")

    def validate_referred_for(self, value):
        return self._validate_services(value, "referred_for")

    def validate_preferred_communication_time_of_day(self, value):
        if value in (None, ""):
            return {day: [] for day in WEEKDAYS}
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                "Must be an object mapping weekdays to lists of windows."
            )
        valid_days = set(WEEKDAYS)
        valid_windows = set(CommunicationTimeOfDay.values)
        normalized = {day: [] for day in WEEKDAYS}
        for day, windows in value.items():
            if day not in valid_days:
                raise serializers.ValidationError(f"Unknown day: {day}")
            if windows is None:
                windows = []
            if not isinstance(windows, list):
                raise serializers.ValidationError(
                    f"{day} must be a list of windows (e.g. ['morning', 'evening'])."
                )
            invalid = [w for w in windows if w not in valid_windows]
            if invalid:
                raise serializers.ValidationError(
                    f"Invalid windows for {day}: {invalid}. "
                    f"Allowed: {sorted(valid_windows)}"
                )
            # de-duplicate while preserving order
            normalized[day] = list(dict.fromkeys(windows))
        return normalized

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        military = validated_data.pop("military_profile", None)
        addresses = validated_data.pop("addresses", None)
        insurances = validated_data.pop("insurances", None)
        client_id = validated_data.pop("client_id")

        client, _ = Client.objects.update_or_create(
            client_id=client_id, defaults=validated_data
        )

        if military is not None:
            MilitaryProfile.objects.update_or_create(
                client=client, defaults=military
            )

        if addresses is not None:
            for addr in addresses:
                Address.objects.update_or_create(
                    client=client,
                    address_type=addr.get("address_type", "current"),
                    defaults=addr,
                )

        if insurances is not None:
            for ins in insurances:
                key = ins.get("insurance_id")
                if key:
                    Insurance.objects.update_or_create(
                        client=client, insurance_id=key, defaults=ins
                    )
                else:
                    # No external insurance_id (e.g. records scraped from the
                    # Unite Us page): dedupe by plan + member id so repeated
                    # syncs update the same row instead of creating duplicates.
                    Insurance.objects.update_or_create(
                        client=client,
                        plan_name=ins.get("plan_name", ""),
                        external_member_id=ins.get("external_member_id", ""),
                        defaults=ins,
                    )

        return client


# ===========================================================================
# Case domain
# ===========================================================================
class CaseSerializer(serializers.ModelSerializer):
    case_id = serializers.UUIDField()
    client_id = serializers.UUIDField()
    previous_case_id = serializers.UUIDField(required=False, allow_null=True)
    originating_provider_id = serializers.UUIDField(required=False, allow_null=True)
    provider_id = serializers.UUIDField(required=False, allow_null=True)
    program_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Case
        exclude = (
            "client",
            "previous_case",
            "originating_provider",
            "provider",
            "program",
            "import_batch",
        )

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        case_id = validated_data.pop("case_id")
        client_id = validated_data.pop("client_id")
        previous_case_id = validated_data.pop("previous_case_id", None)
        originating_provider_id = validated_data.pop("originating_provider_id", None)
        provider_id = validated_data.pop("provider_id", None)
        program_id = validated_data.pop("program_id", None)

        client = Client.objects.filter(pk=client_id).first()
        if client is None:
            raise serializers.ValidationError(
                {"client_id": f"Client {client_id} does not exist. Import the client first."}
            )

        network_id = validated_data.get("network_id")
        network_name = validated_data.get("network_name")
        originating = _resolve_provider(
            originating_provider_id,
            validated_data.get("originating_provider_name"),
            network_id,
            network_name,
        )
        provider = _resolve_provider(
            provider_id, validated_data.get("provider_name"), network_id, network_name
        )
        program = _resolve_program(
            program_id, validated_data.get("program_name"), provider
        )
        previous_case = (
            Case.objects.filter(pk=previous_case_id).first()
            if previous_case_id
            else None
        )

        validated_data.update(
            {
                "client": client,
                "originating_provider": originating,
                "provider": provider,
                "program": program,
                "previous_case": previous_case,
            }
        )
        case, _ = Case.objects.update_or_create(case_id=case_id, defaults=validated_data)
        return case


# ===========================================================================
# Screening domain
# ===========================================================================
class ScreenTemplateSerializer(serializers.ModelSerializer):
    template_id = serializers.UUIDField()
    parent_template_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = ScreenTemplate
        exclude = ("parent_template",)


class QuestionSerializer(serializers.ModelSerializer):
    question_id = serializers.UUIDField()
    parent_question_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Question
        exclude = ("template", "parent_question")


class QuestionOptionSerializer(serializers.ModelSerializer):
    question_option_id = serializers.UUIDField()
    parent_question_option_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = QuestionOption
        exclude = ("question", "parent_question_option")


class AnswerSerializer(serializers.ModelSerializer):
    answer_id = serializers.UUIDField()
    question = QuestionSerializer(required=False, allow_null=True)
    question_option = QuestionOptionSerializer(required=False, allow_null=True)

    class Meta:
        model = Answer
        exclude = ("screening", "eligibility")


class IdentifiedSocialNeedSerializer(serializers.ModelSerializer):
    identified_social_need_id = serializers.UUIDField()

    class Meta:
        model = IdentifiedSocialNeed
        exclude = ("screening",)


class VerifiedSocialNeedSerializer(serializers.ModelSerializer):
    verified_social_need_id = serializers.UUIDField()

    class Meta:
        model = VerifiedSocialNeed
        exclude = ("screening",)


def _upsert_template(data):
    if not data:
        return None
    data = dict(data)
    tid = data.pop("template_id")
    parent_id = data.pop("parent_template_id", None)
    if parent_id:
        data["parent_template"] = ScreenTemplate.objects.filter(pk=parent_id).first()
    obj, _ = ScreenTemplate.objects.update_or_create(template_id=tid, defaults=data)
    return obj


def _upsert_question(data, template):
    if not data:
        return None
    data = dict(data)
    qid = data.pop("question_id")
    parent_id = data.pop("parent_question_id", None)
    data["template"] = template
    if parent_id:
        data["parent_question"] = Question.objects.filter(pk=parent_id).first()
    obj, _ = Question.objects.update_or_create(question_id=qid, defaults=data)
    return obj


def _upsert_option(data, question):
    if not data:
        return None
    data = dict(data)
    oid = data.pop("question_option_id")
    parent_id = data.pop("parent_question_option_id", None)
    data["question"] = question
    if parent_id:
        data["parent_question_option"] = QuestionOption.objects.filter(pk=parent_id).first()
    obj, _ = QuestionOption.objects.update_or_create(question_option_id=oid, defaults=data)
    return obj


class ScreeningSerializer(serializers.ModelSerializer):
    enhanced_screen_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    case_id = serializers.UUIDField(required=False, allow_null=True)
    parent_screen_id = serializers.UUIDField(required=False, allow_null=True)
    related_screen_id = serializers.UUIDField(required=False, allow_null=True)
    template = ScreenTemplateSerializer(required=False, allow_null=True)
    answers = AnswerSerializer(many=True, required=False)
    identified_social_needs = IdentifiedSocialNeedSerializer(many=True, required=False)
    verified_social_needs = VerifiedSocialNeedSerializer(many=True, required=False)

    class Meta:
        model = Screening
        exclude = ("client", "case", "parent_screen", "related_screen", "import_batch")

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        screen_id = validated_data.pop("enhanced_screen_id")
        subject_id = validated_data.get("subject_id")
        case_id = validated_data.pop("case_id", None)
        parent_screen_id = validated_data.pop("parent_screen_id", None)
        related_screen_id = validated_data.pop("related_screen_id", None)
        template_data = validated_data.pop("template", None)
        answers = validated_data.pop("answers", None)
        identified = validated_data.pop("identified_social_needs", None)
        verified = validated_data.pop("verified_social_needs", None)

        template = _upsert_template(template_data)

        validated_data["template"] = template
        validated_data["client"] = Client.objects.filter(pk=subject_id).first()
        validated_data["case"] = (
            Case.objects.filter(pk=case_id).first() if case_id else None
        )
        validated_data["parent_screen"] = (
            Screening.objects.filter(pk=parent_screen_id).first()
            if parent_screen_id
            else None
        )
        validated_data["related_screen"] = (
            Screening.objects.filter(pk=related_screen_id).first()
            if related_screen_id
            else None
        )

        screening, _ = Screening.objects.update_or_create(
            enhanced_screen_id=screen_id, defaults=validated_data
        )

        if answers is not None:
            for ans in answers:
                ans = dict(ans)
                question = _upsert_question(ans.pop("question", None), template)
                option = _upsert_option(ans.pop("question_option", None), question)
                aid = ans.pop("answer_id")
                ans["screening"] = screening
                ans["question"] = question
                ans["question_option"] = option
                Answer.objects.update_or_create(answer_id=aid, defaults=ans)

        if identified is not None:
            for need in identified:
                need = dict(need)
                nid = need.pop("identified_social_need_id")
                need["screening"] = screening
                IdentifiedSocialNeed.objects.update_or_create(
                    identified_social_need_id=nid, defaults=need
                )

        if verified is not None:
            for need in verified:
                need = dict(need)
                nid = need.pop("verified_social_need_id")
                need["screening"] = screening
                VerifiedSocialNeed.objects.update_or_create(
                    verified_social_need_id=nid, defaults=need
                )

        return screening


# ===========================================================================
# Eligibility domain
# ===========================================================================
class EligibilitySerializer(serializers.ModelSerializer):
    eligibility_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    case_id = serializers.UUIDField(required=False, allow_null=True)
    answers = AnswerSerializer(many=True, required=False)

    class Meta:
        model = Eligibility
        exclude = ("client", "case", "import_batch")

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        eid = validated_data.pop("eligibility_id")
        subject_id = validated_data.get("subject_id")
        case_id = validated_data.pop("case_id", None)
        answers = validated_data.pop("answers", None)
        validated_data["client"] = Client.objects.filter(pk=subject_id).first()
        validated_data["case"] = (
            Case.objects.filter(pk=case_id).first() if case_id else None
        )
        obj, _ = Eligibility.objects.update_or_create(
            eligibility_id=eid, defaults=validated_data
        )

        if answers is not None:
            for ans in answers:
                ans = dict(ans)
                question = _upsert_question(ans.pop("question", None), None)
                option = _upsert_option(ans.pop("question_option", None), question)
                aid = ans.pop("answer_id")
                ans["eligibility"] = obj
                ans["question"] = question
                ans["question_option"] = option
                Answer.objects.update_or_create(answer_id=aid, defaults=ans)

        return obj
