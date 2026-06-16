import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .services import catalog

logger = logging.getLogger(__name__)

from .models import (
    WEEKDAYS,
    Address,
    Case,
    Client,
    Assessment,
    ContractedService,
    CommunicationChannel,
    CommunicationTimeOfDay,
    IdentifiedSocialNeed,
    Insurance,
    MilitaryProfile,
    Program,
    Provider,
    RecordStatus,
    Screening,
    ServiceType,
    SocialCareCoverage,
    SocialCareCoverageStatus,
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


class SocialCareCoverageSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialCareCoverage
        exclude = ("id", "client")


class ClientSerializer(serializers.ModelSerializer):
    client_id = serializers.UUIDField()
    military_profile = MilitaryProfileSerializer(required=False, allow_null=True)
    addresses = AddressSerializer(many=True, required=False)
    insurances = InsuranceSerializer(many=True, required=False)
    social_care_coverages = SocialCareCoverageSerializer(many=True, required=False)

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

    @staticmethod
    def _validate_code_list(value, field_name, allowed):
        """Validate a multi-select list against an allowed set of codes,
        de-duplicating while preserving order."""
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                f"{field_name} must be a list of values."
            )
        invalid = [v for v in value if v not in allowed]
        if invalid:
            raise serializers.ValidationError(
                f"Invalid {field_name} values: {invalid}. "
                f"Allowed: {sorted(allowed)}"
            )
        return list(dict.fromkeys(value))

    def validate_communication_channels(self, value):
        return self._validate_code_list(
            value, "communication_channels", set(CommunicationChannel.values)
        )

    def validate_preferred_communication_times(self, value):
        return self._validate_code_list(
            value,
            "preferred_communication_times",
            set(CommunicationTimeOfDay.values),
        )

    def validate_preferred_languages(self, value):
        """Free-text language labels; just enforce a clean list of strings."""
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                "preferred_languages must be a list of language names."
            )
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        return list(dict.fromkeys(cleaned))

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

    @staticmethod
    def _is_expired(expired_at):
        """True when an end date is in the past (so coverage is no longer in
        force). A missing end date means "no expiration"."""
        if not expired_at:
            return False
        return expired_at < timezone.now()

    def _upsert(self, validated_data):
        military = validated_data.pop("military_profile", None)
        addresses = validated_data.pop("addresses", None)
        insurances = validated_data.pop("insurances", None)
        social_care_coverages = validated_data.pop("social_care_coverages", None)
        client_id = validated_data.pop("client_id")
        # Non-model flags (ignored by the serializer fields) read from raw input:
        # when set, the incoming list is treated as authoritative and any stored
        # record missing from it is deactivated.
        raw = getattr(self, "initial_data", {}) or {}
        reconcile = bool(raw.get("reconcile_insurances"))
        reconcile_scc = bool(raw.get("reconcile_social_care_coverages"))

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
                    type=addr.get("type", "current"),
                    defaults=addr,
                )

        if insurances is not None:
            seen_pks = []
            for ins in insurances:
                # Status from the end date: no end date or the 9999 sentinel
                # ("never expires") => Active; a past end date => Expired.
                # Otherwise keep the incoming status.
                exp = ins.get("expired_at")
                if exp is None or getattr(exp, "year", None) == 9999:
                    ins["status"] = RecordStatus.ACTIVE
                elif self._is_expired(exp):
                    ins["status"] = RecordStatus.EXPIRED
                key = ins.get("insurance_id")
                if key:
                    obj, _ = Insurance.objects.update_or_create(
                        client=client, insurance_id=key, defaults=ins
                    )
                else:
                    # No external insurance_id (e.g. records scraped from the
                    # Unite Us page): dedupe by plan + member id so repeated
                    # syncs update the same row instead of creating duplicates.
                    obj, _ = Insurance.objects.update_or_create(
                        client=client,
                        plan_name=ins.get("plan_name", ""),
                        external_member_id=ins.get("external_member_id", ""),
                        defaults=ins,
                    )
                seen_pks.append(obj.pk)

            # Authoritative reconcile: a policy stored on the client but absent
            # from this (Unite Us-sourced) payload is no longer in the source, so
            # mark it inactive instead of deleting it (preserves history and
            # expired_at). Manually-verified rows are left untouched. Gated on the
            # explicit flag so an ordinary/partial sync never deactivates records.
            if reconcile:
                (
                    Insurance.objects.filter(client=client)
                    .exclude(pk__in=seen_pks)
                    .exclude(verified=True)
                    .update(status=RecordStatus.INACTIVE)
                )

        if social_care_coverages is not None:
            seen_scc_pks = []
            for scc in social_care_coverages:
                # Auto-derive Expired from the end date.
                if self._is_expired(scc.get("expired_at")):
                    scc["status"] = SocialCareCoverageStatus.EXPIRED
                key = scc.get("coverage_id")
                if key:
                    obj, _ = SocialCareCoverage.objects.update_or_create(
                        client=client, coverage_id=key, defaults=scc
                    )
                else:
                    obj, _ = SocialCareCoverage.objects.update_or_create(
                        client=client,
                        plan_name=scc.get("plan_name", ""),
                        external_member_id=scc.get("external_member_id", ""),
                        defaults=scc,
                    )
                seen_scc_pks.append(obj.pk)

            # Authoritative reconcile: coverage absent from this payload is no
            # longer in the source, so mark it Non-Enrolled (preserves history).
            if reconcile_scc:
                (
                    SocialCareCoverage.objects.filter(client=client)
                    .exclude(pk__in=seen_scc_pks)
                    .exclude(verified=True)
                    .update(status=SocialCareCoverageStatus.NON_ENROLLED)
                )

        return client


# ===========================================================================
# Case domain
# ===========================================================================
class CaseSerializer(serializers.ModelSerializer):
    case_id = serializers.UUIDField()
    subject_id = serializers.UUIDField(required=False, allow_null=True)
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
        )

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        case_id = validated_data.pop("case_id")
        subject_id = validated_data.pop("subject_id", None)
        client_id = validated_data.pop("client_id")
        previous_case_id = validated_data.pop("previous_case_id", None)
        originating_provider_id = validated_data.pop("originating_provider_id", None)
        provider_id = validated_data.pop("provider_id", None)
        program_id = validated_data.pop("program_id", None)

        # Use subject_id if provided, otherwise fall back to client_id
        lookup_id = subject_id or client_id
        client = Client.objects.filter(pk=lookup_id).first()
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
        # Best-effort: build the master Service catalog (service_type linked to
        # its Program). Never let a catalog error break the case save.
        try:
            catalog.upsert_service_from_case(case.service_type, case.program_name)
        except Exception:
            logger.exception("catalog.upsert_service_from_case failed")
        return case


class ContractedServiceSerializer(serializers.ModelSerializer):
    contracted_service_id = serializers.UUIDField()
    case_id = serializers.UUIDField()

    class Meta:
        model = ContractedService
        exclude = ("case",)

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        contracted_service_id = validated_data.pop("contracted_service_id")
        case_id = validated_data.pop("case_id")

        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            raise serializers.ValidationError(
                {"case_id": f"Case {case_id} does not exist. Import the case first."}
            )

        validated_data["case"] = case
        obj, _ = ContractedService.objects.update_or_create(
            contracted_service_id=contracted_service_id, defaults=validated_data
        )
        return obj


# ===========================================================================
# Screening domain
# ===========================================================================
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


class ScreeningSerializer(serializers.ModelSerializer):
    enhanced_screen_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    questions_answers = serializers.JSONField(required=False, default=list)
    identified_social_needs = serializers.JSONField(required=False, default=list)

    class Meta:
        model = Screening
        fields = [
            "enhanced_screen_id",
            "subject_id",
            "screen_created_at",
            "screen_status",
            "screen_type",
            "screen_source",
            "provider_name",
            "performing_organization_name",
            "duration",
            "questions_answers",
            "identified_social_needs",
            "eligible_status",
            "eligible_services",
            "crm_opportunity_id",
            "crm_sync_hash",
            "crm_synced_at",
        ]

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        screen_id = validated_data.pop("enhanced_screen_id")
        subject_id = validated_data.get("subject_id")

        # Link to client if exists
        validated_data["client"] = Client.objects.filter(pk=subject_id).first()

        screening, _ = Screening.objects.update_or_create(
            enhanced_screen_id=screen_id, defaults=validated_data
        )
        # Best-effort: store unique ProgramMainCategory rows from the results.
        try:
            catalog.upsert_main_categories(screening.identified_social_needs)
        except Exception:
            logger.exception("catalog.upsert_main_categories failed")
        return screening


# ===========================================================================
# Assessment domain (formerly Eligibility)
# ===========================================================================
class AssessmentSerializer(serializers.ModelSerializer):
    assessment_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    questions_answers = serializers.JSONField(required=False, default=list)
    eligible_services = serializers.JSONField(required=False, default=list)

    class Meta:
        model = Assessment
        fields = [
            "assessment_id",
            "subject_id",
            "screen_created_at",
            "eligible_status",
            "provider_name",
            "performing_organization_name",
            "duration",
            "questions_answers",
            "eligible_services",
            "crm_opportunity_id",
            "crm_sync_hash",
            "crm_synced_at",
        ]

    @transaction.atomic
    def create(self, validated_data):
        return self._upsert(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return self._upsert(validated_data)

    def _upsert(self, validated_data):
        aid = validated_data.pop("assessment_id")
        subject_id = validated_data.get("subject_id")

        # Link to client if exists
        validated_data["client"] = Client.objects.filter(pk=subject_id).first()

        obj, _ = Assessment.objects.update_or_create(
            assessment_id=aid, defaults=validated_data
        )
        # Best-effort: store unique master Programs from "Client May Be Eligible".
        try:
            catalog.upsert_programs(obj.eligible_services)
        except Exception:
            logger.exception("catalog.upsert_programs failed")
        return obj
