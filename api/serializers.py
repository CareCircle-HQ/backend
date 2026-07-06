import logging
import re
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .services import catalog

logger = logging.getLogger(__name__)

from .models import (
    WEEKDAYS,
    Address,
    Agent,
    Case,
    CaseHouseholdType,
    CaseType,
    Client,
    ClientLevel,
    ClientPhone,
    ClientPhoneSource,
    DietaryRestriction,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    Household,
    HouseholdMember,
    Assessment,
    ContractedService,
    CommunicationChannel,
    CommunicationTimeOfDay,
    IdentifiedSocialNeed,
    Insurance,
    Lead,
    LeadNote,
    MemberDietaryProfile,
    MemberStatus,
    MenuCategory,
    MenuType,
    MilitaryProfile,
    Program,
    ProgramEligibility,
    ProgramMainCategory,
    ProgramPipeline,
    Provider,
    RecordStatus,
    Screening,
    ServiceType,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    TimelineEvent,
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


class ProgramEligibilitySerializer(serializers.ModelSerializer):
    """A member's model-scored eligibility for a Program. ``program`` is
    embedded read-only so a single response carries the program details."""

    program = ProgramSerializer(read_only=True)

    class Meta:
        model = ProgramEligibility
        fields = [
            "id",
            "member",
            "program",
            "eligibility_score",
            "is_eligible",
            "model_version",
            "evaluated_at",
        ]


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


def _safe_update_or_create(model, defaults, **lookup):
    """Like ``Model.objects.update_or_create`` but tolerant of pre-existing
    duplicate rows matching ``lookup``.

    Some dedupe keys aren't unique-constrained (e.g. Insurance keyed by
    client + plan_name + external_member_id), and historical data can contain
    duplicates. Plain ``update_or_create`` raises MultipleObjectsReturned in
    that case; this picks the first match and updates it instead.
    """
    obj = model.objects.filter(**lookup).order_by("pk").first()
    if obj is None:
        return model.objects.create(**{**lookup, **defaults}), True
    for field, value in defaults.items():
        setattr(obj, field, value)
    obj.save()
    return obj, False


class ClientSerializer(serializers.ModelSerializer):
    client_id = serializers.UUIDField()
    military_profile = MilitaryProfileSerializer(required=False, allow_null=True)
    addresses = AddressSerializer(many=True, required=False)
    insurances = InsuranceSerializer(many=True, required=False)
    social_care_coverages = SocialCareCoverageSerializer(many=True, required=False)

    class Meta:
        model = Client
        fields = "__all__"
        # Derived server-side; never written by the client/extension.
        read_only_fields = (
            "is_level", "lifecycle_stage", "lifecycle_stage_at", "is_williamsburg",
        )

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

    @staticmethod
    def _sync_client_phone(client, number, phone_type):
        """Upsert the ext's primary ``client_phone_number`` into the ClientPhone
        table so it shows in the member-profile "Phone Numbers" widget and is
        reachable via caller-ID lookup. Idempotent on (client, normalized); the
        synced number is (re)made primary. A blank/short number is a no-op."""
        normalized = ClientPhone.normalize(number)
        if not normalized:
            return
        label = (phone_type or "").strip()
        phone, created = ClientPhone.objects.get_or_create(
            client=client,
            normalized=normalized,
            defaults={
                "raw": number,
                "label": label,
                "source": ClientPhoneSource.UNITEUS,
            },
        )
        # Keep the stored raw/label fresh from the source without clobbering an
        # agent-set label with a blank.
        updates = {"last_seen_at": timezone.now()}
        if number and phone.raw != number:
            phone.raw = number
            updates["raw"] = phone.raw
        if label and phone.label != label:
            phone.label = label
            updates["label"] = phone.label
        phone.last_seen_at = updates["last_seen_at"]
        phone.save(update_fields=list(updates))
        # The Unite Us primary number heads the list.
        if not phone.is_primary:
            ClientPhone.objects.filter(client=client, is_primary=True).exclude(
                pk=phone.pk
            ).update(is_primary=False)
            phone.is_primary = True
            phone.save(update_fields=["is_primary"])

    def _upsert(self, validated_data):
        military = validated_data.pop("military_profile", None)
        addresses = validated_data.pop("addresses", None)
        insurances = validated_data.pop("insurances", None)
        social_care_coverages = validated_data.pop("social_care_coverages", None)
        # On a partial update (PATCH) the payload may omit client_id; fall back
        # to the instance being updated so we don't KeyError. client_id is still
        # required on create (enforced by the serializer field).
        client_id = validated_data.pop("client_id", None)
        if client_id is None and self.instance is not None:
            client_id = self.instance.client_id
        # Non-model flags (ignored by the serializer fields) read from raw input:
        # when set, the incoming list is treated as authoritative and any stored
        # record missing from it is deactivated.
        raw = getattr(self, "initial_data", {}) or {}
        reconcile = bool(raw.get("reconcile_insurances"))
        reconcile_scc = bool(raw.get("reconcile_social_care_coverages"))

        # Williamsburg exception flag is derived from lead_source. Only set it
        # when lead_source is part of this write (so a partial PATCH that omits
        # lead_source never clears it).
        if "lead_source" in validated_data:
            validated_data["is_williamsburg"] = (
                (validated_data.get("lead_source") or "").strip().lower() == "williamsburg"
            )

        client, _ = Client.objects.update_or_create(
            client_id=client_id, defaults=validated_data
        )

        # Mirror the ext's single Client.client_phone_number into the ClientPhone
        # table (what the member-profile "Phone Numbers" widget + caller-ID read),
        # but only when this write actually carried a phone -- a partial PATCH
        # that omits it must not re-add a number an agent deleted. Idempotent on
        # (client, normalized); made primary so it heads the list.
        if "client_phone_number" in validated_data:
            self._sync_client_phone(
                client,
                validated_data.get("client_phone_number"),
                validated_data.get("phone_type"),
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
                    obj, _ = _safe_update_or_create(
                        Insurance, ins, client=client, insurance_id=key
                    )
                else:
                    # No external insurance_id (e.g. records scraped from the
                    # Unite Us page): dedupe by plan + member id so repeated
                    # syncs update the same row instead of creating duplicates.
                    obj, _ = _safe_update_or_create(
                        Insurance,
                        ins,
                        client=client,
                        plan_name=ins.get("plan_name", ""),
                        external_member_id=ins.get("external_member_id", ""),
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
                    obj, _ = _safe_update_or_create(
                        SocialCareCoverage, scc, client=client, coverage_id=key
                    )
                else:
                    obj, _ = _safe_update_or_create(
                        SocialCareCoverage,
                        scc,
                        client=client,
                        plan_name=scc.get("plan_name", ""),
                        external_member_id=scc.get("external_member_id", ""),
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

        # Saving the profile may have changed the client's household data, which
        # drives each case's Individual/Household classification. Refresh any of
        # this client's cases whose household_type no longer matches.
        new_household_type = derive_household_type(client)
        Case.objects.filter(client=client).exclude(
            household_type=new_household_type
        ).update(household_type=new_household_type)

        # NOTE: the client's household is NOT created here. It is created when
        # an Internal Service case is saved (see CaseSerializer), since a
        # household only matters once the client has an internal service to be
        # verified/delivered for.
        return client


# ===========================================================================
# Household domain
# ===========================================================================
def ensure_household_with_primary(client):
    """Get-or-create ``client``'s household with the client as primary member.

    If the client already belongs to a household (e.g. added to a relative's
    household), that household is returned unchanged. Otherwise a new household
    is created with the client flagged as its primary member.
    """
    membership = HouseholdMember.objects.filter(client=client).first()
    if membership is not None:
        return membership.household
    household = Household.objects.create()
    HouseholdMember.objects.create(household=household, client=client, is_primary=True)
    return household


@transaction.atomic
def sync_household_members(client, enrollment=None):
    """Reconcile a household's two member sources so every member -- however
    added -- lands in the SAME place.

    A household member can be created two ways: the extension's household picker
    (``ClientViewSet.household_add`` -> a ``HouseholdMember`` roster row) or the
    verification wizard (-> both a ``HouseholdMember`` row AND a
    ``MemberDietaryProfile`` on the enrollment). Only ``MemberDietaryProfile``
    rows carry dietary/menu/status and surface on the CRM Household tab, so a
    member added via the picker alone is invisible there.

    This makes the two converge for the client's active enrollment:
      1. every roster member (``HouseholdMember``) gets a ``MemberDietaryProfile``
         on the enrollment (empty dietary, Standard menu, active) if missing, so
         they appear and are editable; the shared delivery address / service /
         cadence already live once on the enrollment and apply to all;
      2. every profiled member is tied into the roster (a ``HouseholdMember``
         row) unless they already belong to a household.

    Idempotent. Returns the number of dietary profiles created.
    """
    if enrollment is None:
        # Local import: portal.serializers imports this module, so importing it
        # at module load would be circular.
        from .portal.serializers import active_enrollment
        enrollment = active_enrollment(client)
    if enrollment is None:
        return 0

    household = enrollment.household
    if household is None:
        membership = (
            HouseholdMember.objects
            .filter(client=enrollment.client)
            .select_related("household")
            .first()
        )
        household = membership.household if membership else None
    if household is None:
        return 0

    roster = list(household.members.select_related("client").all())
    profiles = {p.client_id: p for p in enrollment.member_profiles.all()}
    roster_ids = {hm.client_id for hm in roster}

    created = 0
    # 1) roster -> ensure a dietary profile exists on this enrollment.
    for hm in roster:
        member = hm.client
        if member is None or member.pk in profiles:
            continue
        # New members carry NO default menu type / allergies and start Out of
        # Orbit: an agent must assign a menu type + restrictions, and only then
        # (on save) is the kitchen output computed and the member activated.
        MemberDietaryProfile.objects.create(
            enrollment=enrollment,
            client=member,
            member_name=f"{member.first_name} {member.last_name}".strip(),
            menu_type="",
            status=MemberStatus.OUT_OF_ORBIT,
        )
        created += 1

    # 2) profiled members -> ensure a roster row (one-household-per-client).
    for cid in profiles:
        if not cid or cid in roster_ids:
            continue
        if HouseholdMember.objects.filter(client_id=cid).exists():
            continue
        HouseholdMember.objects.create(
            household=household, client_id=cid, is_primary=False,
        )

    return created


def search_clients(q):
    """Find existing clients by member ID (client UUID) or by Medicaid /
    insurance member ID (``external_member_id``). Used by the household member
    pickers (extension + CRM). Returns lightweight dict rows, each flagged with
    whether the client already belongs to a household."""
    q = (q or "").strip()
    if len(q) < 2:
        return []

    filters = (
        Q(insurances__external_member_id__icontains=q)
        | Q(social_care_coverages__external_member_id__icontains=q)
    )
    # client_id is a UUID column: only match it when q parses as a UUID.
    try:
        filters |= Q(client_id=uuid.UUID(q))
    except (ValueError, AttributeError, TypeError):
        pass

    qs = (
        Client.objects.filter(filters)
        .distinct()
        .prefetch_related("insurances", "social_care_coverages")[:25]
    )

    results = []
    for c in qs:
        member_ids = sorted({
            mid
            for src in (c.insurances.all(), c.social_care_coverages.all())
            for mid in (x.external_member_id for x in src)
            if mid
        })
        results.append({
            "client_id": str(c.client_id),
            "first_name": c.first_name,
            "last_name": c.last_name,
            "date_of_birth": c.date_of_birth.isoformat() if c.date_of_birth else None,
            "member_ids": member_ids,
            "in_household": HouseholdMember.objects.filter(client=c).exists(),
        })
    return results


@transaction.atomic
def add_client_to_household(primary, member_client):
    """Add ``member_client`` to ``primary``'s household, MOVING them out of any
    OTHER household first (one-household-per-client). Mirrors the member into the
    household's active enrollment as a dietary profile so they show + are
    editable on the CRM Household tab. Idempotent. Returns the household.

    Does NOT enforce the family-size cap -- callers that need it (the extension
    picker) check it before calling.
    """
    household = ensure_household_with_primary(primary)

    # Idempotent: already a member of THIS household -> nothing to do.
    if household.members.filter(client=member_client).exists():
        return household

    # If the client is already in ANOTHER household, move them here: detach from
    # the previous household and drop their dietary profile(s) on its enrollments
    # (so the read-side sync won't re-add them there), then clean up if empty.
    existing = HouseholdMember.objects.filter(client=member_client).first()
    if existing is not None:
        old_household = existing.household
        MemberDietaryProfile.objects.filter(
            client=member_client, enrollment__household=old_household
        ).delete()
        existing.delete()
        if not old_household.members.exists():
            old_household.delete()

    HouseholdMember.objects.create(
        household=household, client=member_client, is_primary=False
    )
    sync_household_members(primary)
    return household


class HouseholdMemberSerializer(serializers.ModelSerializer):
    client_id = serializers.UUIDField(source="client.client_id", read_only=True)
    first_name = serializers.CharField(source="client.first_name", read_only=True)
    last_name = serializers.CharField(source="client.last_name", read_only=True)
    date_of_birth = serializers.DateField(source="client.date_of_birth", read_only=True)

    class Meta:
        model = HouseholdMember
        fields = (
            "client_id",
            "first_name",
            "last_name",
            "date_of_birth",
            "is_primary",
            "relationship",
            "added_at",
        )


class HouseholdSerializer(serializers.ModelSerializer):
    members = HouseholdMemberSerializer(many=True, read_only=True)

    class Meta:
        model = Household
        fields = ("household_id", "name", "members", "created_at", "updated_at")


# ===========================================================================
# Enrollment verification domain
# ===========================================================================
_UNSET = object()  # sentinel: distinguish "omitted" from an explicit null on PATCH


def _validate_choice_codes(value, choices_cls, field_name):
    """Validate a multi-select list against a TextChoices set, de-duplicating
    while preserving order. Empty / null normalizes to []."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError(f"{field_name} must be a list of codes.")
    valid = set(choices_cls.values)
    invalid = [v for v in value if v not in valid]
    if invalid:
        raise serializers.ValidationError(
            f"Invalid {field_name} values: {invalid}. Allowed: {sorted(valid)}"
        )
    seen = []
    for v in value:
        if v not in seen:
            seen.append(v)
    return seen


class MemberDietaryProfileSerializer(serializers.ModelSerializer):
    # The participant client UUID (the FK's pk). Readable + writable; resolved
    # to a Client by the parent EnrollmentVerificationSerializer.
    client_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = MemberDietaryProfile
        fields = (
            "id",
            "client_id",
            "member_name",
            "dietary_restrictions",
            "food_allergies",
            "other_dietary_restrictions",
            "meal_category",
            "menu_type",
            "meals_per_delivery",
            "general_verification_notes",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_dietary_restrictions(self, value):
        return _validate_choice_codes(value, DietaryRestriction, "dietary_restrictions")

    def validate_food_allergies(self, value):
        return _validate_choice_codes(value, FoodAllergy, "food_allergies")


class EnrollmentVerificationSerializer(serializers.ModelSerializer):
    """Read/write the verification enrollment plus its per-member answers.

    ``stage`` is read-only here: stage changes (which include the Step-4
    authorization outcome) must go through the ``set-stage`` action so they are
    guarded and recorded on the timeline.
    """

    # Write-only inputs, resolved to FKs in create/update.
    client_id = serializers.UUIDField(write_only=True, required=False)
    household_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)
    case_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)
    delivery_address_id = serializers.IntegerField(
        write_only=True, required=False, allow_null=True
    )
    members = MemberDietaryProfileSerializer(
        many=True, source="member_profiles", required=False
    )

    class Meta:
        model = EnrollmentVerification
        fields = (
            "id",
            "client_id",
            "household_id",
            "case_id",
            "delivery_address_id",
            "stage",
            "program_name",
            "service_type",
            "call_transfer_answered",
            "household_size",
            "delivery_weekdays",
            "is_family_verified",
            "medicaid_type_verified",
            "delivery_address_verified",
            "code",
            "renewal_number",
            "stage_at",
            "opened_at",
            "closed_at",
            "note",
            "members",
        )
        read_only_fields = ("id", "stage", "code", "stage_at", "opened_at", "closed_at")

    def validate_members(self, value):
        if value is None:
            return value
        if len(value) > 10:
            raise serializers.ValidationError("A household may have at most 10 members.")
        if len(value) < 1:
            raise serializers.ValidationError("At least 1 member is required.")
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["client_id"] = str(instance.client_id) if instance.client_id else None
        data["household_id"] = str(instance.household_id) if instance.household_id else None
        data["case_id"] = str(instance.case_id) if instance.case_id else None
        data["stage_display"] = instance.get_stage_display()
        data["delivery_address"] = (
            AddressSerializer(instance.delivery_address).data
            if instance.delivery_address_id
            else None
        )
        return data

    def _sync_members(self, enrollment, members):
        """Full-replace the enrollment's per-member dietary profiles."""
        from api.services.catalog import menu_type_for_member

        enrollment.member_profiles.all().delete()
        seen_clients = set()
        for m in members:
            client_uuid = m.get("client_id")
            if client_uuid is not None and client_uuid in seen_clients:
                continue  # ignore duplicate participant rows
            member_client = (
                Client.objects.filter(pk=client_uuid).first() if client_uuid else None
            )
            if client_uuid is not None:
                seen_clients.add(client_uuid)
            name = m.get("member_name") or (
                f"{member_client.first_name} {member_client.last_name}".strip()
                if member_client
                else ""
            )
            MemberDietaryProfile.objects.create(
                enrollment=enrollment,
                client=member_client,
                member_name=name,
                dietary_restrictions=m.get("dietary_restrictions", []),
                food_allergies=m.get("food_allergies", []),
                other_dietary_restrictions=m.get("other_dietary_restrictions", ""),
                meal_category=m.get("meal_category", ""),
                # Derive menu type from dietary data when not explicitly sent.
                menu_type=m.get("menu_type") or menu_type_for_member(
                    food_allergies=m.get("food_allergies", []),
                    meal_category=m.get("meal_category", ""),
                ),
                meals_per_delivery=m.get("meals_per_delivery"),
                general_verification_notes=m.get("general_verification_notes", ""),
            )

    @transaction.atomic
    def create(self, validated_data):
        members = validated_data.pop("member_profiles", [])
        cid = validated_data.pop("client_id", None)
        hid = validated_data.pop("household_id", None)
        case_id = validated_data.pop("case_id", None)
        aid = validated_data.pop("delivery_address_id", None)

        client = Client.objects.filter(pk=cid).first() if cid else None
        if client is None:
            raise serializers.ValidationError(
                {"client_id": "A valid client_id is required."}
            )
        case = Case.objects.filter(pk=case_id).first() if case_id else None
        # Snapshot the case's Service Type when the ext didn't send one.
        if case is not None and not validated_data.get("service_type"):
            validated_data["service_type"] = case.service_type
        enrollment = EnrollmentVerification.objects.create(
            client=client,
            household=Household.objects.filter(pk=hid).first() if hid else None,
            case=case,
            delivery_address=Address.objects.filter(pk=aid).first() if aid else None,
            **validated_data,
        )
        self._sync_members(enrollment, members)
        return enrollment

    @transaction.atomic
    def update(self, instance, validated_data):
        members = validated_data.pop("member_profiles", None)
        cid = validated_data.pop("client_id", None)
        hid = validated_data.pop("household_id", _UNSET)
        case_id = validated_data.pop("case_id", _UNSET)
        aid = validated_data.pop("delivery_address_id", _UNSET)

        if cid:
            client = Client.objects.filter(pk=cid).first()
            if client is not None:
                instance.client = client
        if hid is not _UNSET:
            instance.household = Household.objects.filter(pk=hid).first() if hid else None
        if case_id is not _UNSET:
            instance.case = Case.objects.filter(pk=case_id).first() if case_id else None
        if aid is not _UNSET:
            instance.delivery_address = (
                Address.objects.filter(pk=aid).first() if aid else None
            )
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if members is not None:
            self._sync_members(instance, members)
        return instance


# ===========================================================================
# Case domain
# Map a ProgramPipeline.case_category value to a Case.case_type. Keys are
# casefolded; both singular/plural spellings from the source data are accepted.
_PIPELINE_CATEGORY_TO_CASE_TYPE = {
    "navigation": CaseType.NAVIGATION,
    "eligibility": CaseType.ELIGIBILITY,
    "internal service": CaseType.INTERNAL_SERVICE,
    "internal services": CaseType.INTERNAL_SERVICE,
    "external service": CaseType.EXTERNAL_SERVICE,
    "external services": CaseType.EXTERNAL_SERVICE,
}


def derive_case_type_from_pipeline(program_name):
    """Classify a case by matching its program_name against the ProgramPipeline
    table and mapping the matched row's case_category to a CaseType.

    The match is case-insensitive and whitespace-trimmed so minor differences
    between the Unite Us program name and the seeded table still resolve.
    Returns None when program_name is blank, no row matches, or the matched
    category isn't recognized — callers then fall back to the service_type
    heuristic.
    """
    pn = (program_name or "").strip()
    if not pn:
        return None
    row = ProgramPipeline.objects.filter(program_name__iexact=pn).first()
    if row is None:
        return None
    return _PIPELINE_CATEGORY_TO_CASE_TYPE.get((row.case_category or "").strip().casefold())


# Service subtypes (stored in ``Case.service_type``) that ARE our internal
# meal/box service. Internal-service status is keyed on these subtypes, NOT on
# the program name -- they map 1:1 to the meal/box programs and also catch
# cases whose program_name is blank.
INTERNAL_SERVICE_SUBTYPES = frozenset({
    "medically tailored meals",
    "produce prescription/voucher",
})


def derive_case_type(service_type, program_name=None):
    """Classify a case.

    Internal Service is identified by the meal/box service subtype (stored in
    ``service_type``): Medically Tailored Meals or Produce Prescription/Voucher.
    Otherwise the type comes from the ``program_name``'s ProgramPipeline
    category (Eligibility / Navigation / External Service); a blank or unmatched
    program is Navigation.

    Returns None only when there's nothing to classify on (no internal subtype,
    no program match, and no service_type), so callers leave the existing value
    / model default untouched.
    """
    st = (service_type or "").strip()
    if st.casefold() in INTERNAL_SERVICE_SUBTYPES:
        return CaseType.INTERNAL_SERVICE
    from_pipeline = derive_case_type_from_pipeline(program_name)
    if from_pipeline is not None:
        return from_pipeline
    if not st:
        return None
    return CaseType.NAVIGATION


def derive_household_type(client):
    """Individual vs Household from the client's household data: a household
    when the client is flagged as a family or has more than one member."""
    is_household = bool(getattr(client, "is_a_family", False)) or (
        (getattr(client, "household_size", None) or 0) > 1
    )
    return (
        CaseHouseholdType.HOUSEHOLD if is_household else CaseHouseholdType.INDIVIDUAL
    )


# Matches a "Level 1"/"Level 2" marker in an eligible service name, tolerant of
# spacing (e.g. "Level2") and word boundaries (won't match "Level 12").
_LEVEL_2_RE = re.compile(r"\blevel\s*2\b", re.IGNORECASE)
_LEVEL_1_RE = re.compile(r"\blevel\s*1\b", re.IGNORECASE)


def derive_client_level(eligible_services):
    """Scan an assessment's eligible service names for a "Level 1"/"Level 2"
    marker and return the matching ClientLevel value, or None when neither is
    present. Level 2 takes precedence over Level 1.
    """
    has_level_1 = has_level_2 = False
    for raw in eligible_services or []:
        name = raw.get("name") or raw.get("code") if isinstance(raw, dict) else raw
        if not isinstance(name, str):
            continue
        if _LEVEL_2_RE.search(name):
            has_level_2 = True
        elif _LEVEL_1_RE.search(name):
            has_level_1 = True
    if has_level_2:
        return ClientLevel.LEVEL_2
    if has_level_1:
        return ClientLevel.LEVEL_1
    return None


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

        # Auto-classify on every create/update. An explicit value in the payload
        # wins (e.g. a manually-set External Service); otherwise derive from the
        # service_type (case_type) and the client's household data (household_type).
        if "case_type" not in validated_data:
            derived_type = derive_case_type(
                validated_data.get("service_type"),
                validated_data.get("program_name"),
            )
            if derived_type is not None:
                validated_data["case_type"] = derived_type
        if "household_type" not in validated_data:
            validated_data["household_type"] = derive_household_type(client)

        # Capture the stored status + authorization BEFORE the write so callers
        # can tell what changed (the internal-service denial ticket below, and
        # the case-change tracking the viewset runs after save).
        _prev = (
            Case.objects.filter(pk=case_id)
            .values("case_status", "service_authorization_status")
            .first()
        )
        _prev_status = _prev["case_status"] if _prev else None
        _prev_auth = _prev["service_authorization_status"] if _prev else None
        case, _ = Case.objects.update_or_create(case_id=case_id, defaults=validated_data)
        # Stash the pre-save values on the instance so the write path (e.g.
        # CaseViewSet, extension) can record the change + attribute it, without
        # re-querying. Import/daily-sync paths capture prev themselves.
        case._prev_status = _prev_status
        case._prev_auth = _prev_auth
        # Best-effort: build the master Service catalog (service_type linked to
        # its Program). Never let a catalog error break the case save.
        try:
            catalog.upsert_service_from_case(case.service_type, case.program_name)
        except Exception:
            logger.exception("catalog.upsert_service_from_case failed")
        # Best-effort: for Internal Service cases, link the program to its
        # ProductType (Meals/Boxes) by keyword in the program name.
        try:
            if case.case_type == CaseType.INTERNAL_SERVICE and case.program_id:
                catalog.assign_product_type_for_internal_service(case.program)
        except Exception:
            logger.exception("catalog.assign_product_type_for_internal_service failed")
        # Internal Service cases are the ones that go through verification and
        # meal/box delivery, so ensure the client has a household (with this
        # client as primary) here — on case save — rather than on profile save.
        # Get-or-create, so re-saving the case never duplicates the household.
        try:
            if case.case_type == CaseType.INTERNAL_SERVICE:
                ensure_household_with_primary(client)
        except Exception:
            logger.exception("ensure_household_with_primary failed for internal service case")
        # Internal-service authorization full-stop rule: a client with a SINGLE
        # internal-service (meal/box) case that is denied is auto-paused (On Hold)
        # so service stops and they drop off kitchen assignment; a later favorable
        # authorization resumes them. Two-plus internal-service cases are never a
        # full stop. Raise a HIGH follow-up ticket the first time the sole case
        # flips to denied. Best-effort: never let this break the case save.
        try:
            if case.case_type == CaseType.INTERNAL_SERVICE:
                from .models import (
                    ServiceAuthorizationStatus,
                    TicketSeverity,
                    TicketTypeCode,
                )
                from .services import tickets
                from .services.lifecycle import (
                    reconcile_internal_service_authorization,
                )

                outcome = reconcile_internal_service_authorization(client)
                newly_denied = (
                    outcome["sole_denied"]
                    and _prev_auth != ServiceAuthorizationStatus.DENIED
                    and case.service_authorization_status
                    == ServiceAuthorizationStatus.DENIED
                )
                if newly_denied:
                    tickets.open_ticket(
                        TicketTypeCode.SYSTEM_CHANGE_DETECTED,
                        reason=(
                            f"The member's only internal-service (meal/box) case "
                            f"{case.case_id} was denied. Service has been paused "
                            f"(placed On Hold) and the member removed from kitchen "
                            f"assignment. Confirm the denial and follow up with the "
                            f"member; if it is overturned, resume service."
                        ),
                        severity=TicketSeverity.HIGH,
                        client=client,
                        case=case,
                    )
        except Exception:
            logger.exception(
                "internal-service authorization reconcile failed for case %s", case_id
            )
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
            "form_name",
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
        # Derive the client's service level from the eligible service names
        # carrying a "Level 1"/"Level 2" marker. Only set when detected so a
        # marker-less assessment doesn't wipe a level set by another one.
        if obj.client:
            level = derive_client_level(obj.eligible_services)
            if level and obj.client.is_level != level:
                obj.client.is_level = level
                obj.client.save(update_fields=["is_level"])
        return obj


# ===========================================================================
# Timeline (central history)
# ===========================================================================
class TimelineEventSerializer(serializers.ModelSerializer):
    """Read-only view of a timeline event for the manager dashboard.

    ``entity_type`` / ``entity_id`` give the frontend a stable deep-link to the
    underlying record (screening, assessment, case, insurance, …) without
    embedding the whole object.
    """

    entity_type = serializers.SerializerMethodField()
    entity_id = serializers.CharField(source="object_id", read_only=True)

    class Meta:
        model = TimelineEvent
        fields = [
            "id",
            "event_type",
            "occurred_at",
            "title",
            "subtitle",
            "badge_text",
            "badge_tone",
            "source",
            "actor",
            "renewal_number",
            "enrollment",
            "case",
            "entity_type",
            "entity_id",
            "metadata",
            "created_at",
        ]

    def get_entity_type(self, obj):
        return obj.content_type.model if obj.content_type_id else None


class LeadNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadNote
        fields = ["id", "author_name", "body", "created_at"]
        read_only_fields = ["id", "author_name", "created_at"]


class LeadSerializer(serializers.ModelSerializer):
    """Serialize/validate a public-funnel :class:`Lead`.

    Step-1 fields (name, phone, ZIP, Medicaid status) are required on create
    along with the legal disclaimer; step-2 fields are optional and typically
    PATCHed in later. ``disclaimer_accepted_at`` is server-stamped when consent
    is recorded; ``status`` is managed internally (read-only to the funnel).

    ``assigned_to`` links the screener following up on the lead; ``converted_client``
    links the Client the lead became (set on conversion) for funnel tracking.
    """

    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=Agent.objects.all(), allow_null=True, required=False
    )
    assigned_to_name = serializers.SerializerMethodField()
    converted_client = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), allow_null=True, required=False
    )
    converted_client_name = serializers.SerializerMethodField()
    interested_programs = serializers.PrimaryKeyRelatedField(
        many=True, queryset=ProgramMainCategory.objects.all(), required=False
    )
    interested_program_names = serializers.SerializerMethodField()
    notes = LeadNoteSerializer(many=True, read_only=True)

    class Meta:
        model = Lead
        fields = [
            "lead_id",
            # Step 1 — required capture
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "zip_code",
            "medicaid_enrollment",
            "disclaimer_accepted",
            "disclaimer_accepted_at",
            # Step 2 — optional enrichment
            "medicaid_id",
            "date_of_birth",
            "additional_details",
            "household_size",
            "preferred_contact_method",
            "do_not_contact",
            # Assignment & conversion tracking
            "assigned_to",
            "assigned_to_name",
            "converted_client",
            "converted_client_name",
            "interested_programs",
            "interested_program_names",
            "notes",
            # Internal / metadata
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "lead_id",
            "disclaimer_accepted_at",
            "status",
            "created_at",
            "updated_at",
        ]

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.name if obj.assigned_to_id else None

    def get_converted_client_name(self, obj):
        c = obj.converted_client
        return f"{c.first_name} {c.last_name}".strip() if obj.converted_client_id else None

    def get_interested_program_names(self, obj):
        return [c.name for c in obj.interested_programs.all()]

    def validate_phone_number(self, value):
        digits = re.sub(r"\D", "", value or "")
        if len(digits) < 10:
            raise serializers.ValidationError("Enter a valid phone number.")
        return (value or "").strip()

    def validate_zip_code(self, value):
        v = (value or "").strip()
        if not re.fullmatch(r"\d{5}(-\d{4})?", v):
            raise serializers.ValidationError("Enter a valid 5-digit ZIP code.")
        return v

    def validate(self, attrs):
        # Submitting step 1 ("Check My Eligibility") records consent — so the
        # disclaimer must be accepted to create a lead.
        if self.instance is None and not attrs.get("disclaimer_accepted"):
            raise serializers.ValidationError(
                {"disclaimer_accepted": "You must accept the disclaimer to continue."}
            )
        return attrs

    def _stamp_consent(self, validated_data, instance=None):
        # Record when consent was given the first time it flips to accepted.
        if validated_data.get("disclaimer_accepted"):
            already = getattr(instance, "disclaimer_accepted_at", None)
            if not already:
                validated_data["disclaimer_accepted_at"] = timezone.now()
        return validated_data

    def create(self, validated_data):
        validated_data = self._stamp_consent(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data = self._stamp_consent(validated_data, instance)
        return super().update(instance, validated_data)
