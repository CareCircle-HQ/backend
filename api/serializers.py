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
    CaseStatus,
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
    Note,
    NoteSource,
    OrderSchedule,
    ActiveProgram,
    Program,
    ProgramEligibility,
    ProgramMainCategory,
    Provider,
    RecordStatus,
    Screening,
    ServiceAuthorizationStatus,
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
    existing = Program.objects.filter(program_id=program_id).first()
    # Programs are only ADDED for the allowed organization (Met Council - SCN -
    # PHS). A program from any other provider is ignored (the case's program FK
    # is left null); a program we already know is still updated.
    if existing is None and not catalog.is_allowed_program_provider(provider):
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

        # Unite Us person-migration guard: if this incoming id was already
        # migrated away to a surviving canonical client, write to the SURVIVOR
        # instead of resurrecting the retired id (which would recreate the
        # duplicate we just merged).
        survivor = Client.objects.filter(migrated_from_id=str(client_id)).first()
        if survivor is not None:
            for k, v in validated_data.items():
                setattr(survivor, k, v)
            survivor.save()
            client = survivor
        else:
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
                # The source-provided status (e.g. insurance_record_status) is
                # AUTHORITATIVE: a policy the source marks Active is NOT flipped
                # to Expired just because its stored end date is in the past. Only
                # DERIVE the status from the end date when the source didn't send
                # one: no end date / the 9999 sentinel ("never expires") => Active;
                # a past end date => Expired.
                if not ins.get("status"):
                    exp = ins.get("expired_at")
                    if exp is None or getattr(exp, "year", None) == 9999:
                        ins["status"] = RecordStatus.ACTIVE
                    elif self._is_expired(exp):
                        ins["status"] = RecordStatus.EXPIRED
                # End date is authoritative for an ACTIVE policy: if the source
                # says the policy is Active but sends NO end date ("End --" = no
                # expiry / currently in force), explicitly clear any stale past
                # end date already stored -- otherwise a renewed policy keeps its
                # old expired date and reads as expired by the date-based gate.
                if ins.get("status") == RecordStatus.ACTIVE and not ins.get("expired_at"):
                    ins["expired_at"] = None
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
                # The source-provided status (insurance_status) is AUTHORITATIVE.
                # Only derive Expired from the end date when the source didn't
                # send a status -- an Enrolled coverage is not flipped to Expired
                # just because its stored end date is in the past.
                if not scc.get("status") and self._is_expired(scc.get("expired_at")):
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
        # (together with each case's own program "(Household)" pathway) drives
        # the Individual/Household classification. Recompute PER CASE, since the
        # program token differs between a client's cases.
        for case in Case.objects.filter(client=client):
            ht = derive_household_type(client, case.program_name)
            if case.household_type != ht:
                Case.objects.filter(pk=case.pk).update(household_type=ht)

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
def ensure_primary_of_own_household(client):
    """Ensure ``client`` is the PRIMARY of their own household.

    A client who holds their own Internal Service (meal/box) case goes through
    verification + delivery as a household head, so they must be the primary of
    their household AND every enrollment they own must be anchored to that
    household. Two mis-anchorings are healed:

    1. The client is a NON-primary member of a (shared) household -- e.g. an
       agent added them as a relative's dependent -- so split them out.
    2. The client owns an enrollment whose ``household`` points at a RELATIVE's
       household (e.g. from a duplicate import), even while the client is already
       primary of their own household. Left there, ``active_enrollment`` resolves
       that relative's roster, so the Household tab shows the relative's members
       and never the client as primary.

    Every enrollment the client owns is re-homed to their primary household, and
    any relatives dragged onto those enrollments by household sync are detached
    (their profiles belong to their own household). Idempotent.
    """
    membership = (
        HouseholdMember.objects.filter(client=client)
        .select_related("household")
        .first()
    )
    # (1) If the client is a NON-primary member of a shared household, split them
    # out of it first (they leave that roster entirely).
    left_household = None
    if membership is not None and not membership.is_primary:
        left_household = membership.household
        membership.delete()
    # The client's OWN primary household (their existing one, or a fresh one).
    household = ensure_household_with_primary(client)
    # (2) Re-home every enrollment the client owns into their primary household.
    for enr in list(
        EnrollmentVerification.objects.filter(client=client).exclude(household=household)
    ):
        # Detach relatives dragged onto this enrollment by household sync: their
        # dietary profiles belong to their own household's enrollment, not the
        # client's. The client's own profile (if any) is kept.
        MemberDietaryProfile.objects.filter(enrollment=enr).exclude(
            client=client
        ).delete()
        enr.household = household
        enr.save(update_fields=["household"])
        # The enrollment's order schedules carry their OWN household FK
        # (PROTECT); re-home them alongside the enrollment so they follow the
        # client and don't pin the old household open.
        OrderSchedule.objects.filter(enrollment=enr).update(household=household)
    # Drop the client's dietary profile from any enrollments that STAYED in the
    # household they just left (they're no longer a member there).
    if left_household is not None:
        MemberDietaryProfile.objects.filter(
            client=client, enrollment__household=left_household
        ).delete()
        # Only delete the household they left if it is now truly empty. A
        # malformed household can retain a RELATIVE's enrollment / order
        # schedules even with no member rows -- deleting it would orphan (or,
        # for PROTECT'd orders, error on) that live data.
        still_in_use = (
            left_household.members.exists()
            or left_household.enrollment_verifications.exists()
            or left_household.orders.exists()
        )
        if not still_in_use:
            left_household.delete()
    return household


@transaction.atomic
def sync_household_members(client, enrollment=None, agent=None):
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
        # ALWAYS carry a member's dietary INFORMATION forward from their most
        # recent OTHER enrollment profile (e.g. a superseded enrollment after a
        # governing-case change): menu type, dietary restrictions, food allergies,
        # other restrictions, verification notes and meal category don't change
        # with the case/meal type. Without this, a member who had a full profile
        # on the closed enrollment reappeared here BLANK (losing their menu /
        # dietary). Only a genuinely NEW member (no prior profile anywhere) starts
        # blank -- their menu is chosen at kitchen assignment.
        #
        # NB: service STATUS is deliberately NOT carried -- it's governed by the
        # scope rules elsewhere (a Household->Individual switch PAUSES the extra
        # members; Individual->Household re-activates them). Carrying the prior
        # status here would fight those rules (e.g. re-activate a member the
        # individual switch just paused). Status stays at the model default and
        # the scope/activation logic sets it.
        prior = (
            MemberDietaryProfile.objects.filter(client=member)
            .exclude(enrollment=enrollment)
            .order_by("-enrollment__opened_at")
            .first()
        )
        carried = {"menu_type": ""}
        if prior is not None:
            carried = {
                "menu_type": prior.menu_type,
                "dietary_restrictions": prior.dietary_restrictions,
                "food_allergies": prior.food_allergies,
                "other_dietary_restrictions": prior.other_dietary_restrictions,
                "meal_category": prior.meal_category,
                "general_verification_notes": prior.general_verification_notes,
            }
        profile = MemberDietaryProfile.objects.create(
            enrollment=enrollment,
            client=member,
            member_name=f"{member.first_name} {member.last_name}".strip(),
            **carried,
        )
        created += 1
        # Delivery Coverage takes priority over the default Out of Orbit: if this
        # household's delivery ZIP (or the new member's own primary ZIP) is
        # outside the coverage area — the same block that already put the
        # existing members Out of Range — the new member inherits Out of Range
        # too, since a menu type can't fix a geographic block.
        from .services.service_area import member_excluded_info, service_area_note_body
        oor_zip, oor_source = member_excluded_info(profile)
        # Attribute the acting agent (who added the member) so the note author
        # and the timeline actor show WHO performed the action instead of blank.
        agent_author = (agent.name if agent else "") or ""
        # Prefer the agent code (resolved to a name in the UI); fall back to the
        # agent's name for code-less agents so the actor isn't blank.
        if agent and agent.agent_code:
            agent_actor = f"agent:{agent.agent_code}"
        elif agent_author:
            agent_actor = f"user:{agent_author}"
        else:
            agent_actor = ""
        if oor_zip:
            profile.status = MemberStatus.OUT_OF_RANGE
            profile.save(update_fields=["status"])
            reason = service_area_note_body(oor_zip, oor_source)
            try:
                Note.objects.create(
                    client=member, source=NoteSource.SYSTEM,
                    author_name=agent_author, body=reason,
                )
            except Exception:
                logger.warning("household member note failed", exc_info=True)
            try:
                from .services import timeline
                timeline.event_for_out_of_range(
                    profile, enrollment=enrollment, reason=reason,
                    zip_code=oor_zip, actor=agent_actor,
                )
            except Exception:
                logger.warning("household member out-of-range event failed", exc_info=True)
            continue
        # New members stay PENDING (the model default): they are only activated
        # by the kitchen-assignment meal rule (or the explicit "reactivate" edit
        # once an agent gives them a menu type). A PENDING member is excluded from
        # every delivery schedule / Purchase Order, so this is safe whether or not
        # the household already has a kitchen. We leave an informational system
        # note when the household is already served, so it's clear the new member
        # needs configuration before they can join deliveries.
        if not enrollment.kitchen_id:
            continue
        reason = (
            "New member added outside of the verification process. "
            "This member needs a menu type and dietary preferences before they "
            "can be activated (kept Pending)."
        )
        try:
            Note.objects.create(
                client=member, source=NoteSource.SYSTEM,
                author_name=agent_author, body=reason,
            )
        except Exception:
            logger.warning("household member note failed", exc_info=True)

    # 2) profiled members -> ensure a roster row (one-household-per-client).
    # A REMOVED profile is history (the member was split into their own case);
    # never re-add them to this household's roster.
    for cid, prof in profiles.items():
        if not cid or cid in roster_ids:
            continue
        if prof.status == MemberStatus.REMOVED:
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
def add_client_to_household(primary, member_client, agent=None):
    """Add ``member_client`` to ``primary``'s household, MOVING them out of any
    OTHER household first (one-household-per-client). Mirrors the member into the
    household's active enrollment as a dietary profile so they show + are
    editable on the CRM Household tab. Idempotent. Returns the household.

    Does NOT enforce the family-size cap -- callers that need it (the extension
    picker) check it before calling.
    """
    household = ensure_household_with_primary(primary)

    # An explicit agent "add" RE-ACTIVATES a member whose profile on this
    # household's enrollment was left as REMOVED (e.g. they were split out into
    # their own case earlier). The REMOVED roster-sync exemption is only meant to
    # stop AUTOMATIC re-adds -- an agent adding them back intends a live member.
    def _reactivate_removed_profiles(mc):
        MemberDietaryProfile.objects.filter(
            client=mc, enrollment__household=household, status=MemberStatus.REMOVED,
        ).update(status=MemberStatus.ACTIVE, status_changed_at=timezone.now())

    # Idempotent: already a member of THIS household -> still reactivate any
    # REMOVED profile (so a split-out member can be re-added) and re-sync.
    if household.members.filter(client=member_client).exists():
        _reactivate_removed_profiles(member_client)
        sync_household_members(primary, agent=agent)
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
    # A re-added member may still carry a REMOVED profile on this household's
    # enrollment from a prior split -- reactivate it so they show + are served.
    _reactivate_removed_profiles(member_client)
    sync_household_members(primary, agent=agent)
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

        # INVARIANT: a client has at most ONE live internal-service enrollment.
        # If one is already IN THE FUNNEL (not terminal and not yet serving),
        # REBIND + refresh it instead of opening a SECOND live row -- a new/renewal
        # case must never fork a duplicate enrollment (which then gets worked
        # independently by verification / nutrition / logistics). ``stage`` is
        # read-only here, so the enrollment keeps its funnel progress; a later
        # set-stage advances it. A SERVING enrollment is left untouched -- a
        # governing-case change for a serving household is carried by reconcile,
        # not overwritten by a fresh verification.
        from api.models import EnrollmentStage
        _TERMINAL_OR_SERVING = [
            EnrollmentStage.CLOSED, EnrollmentStage.CANCELLED,
            EnrollmentStage.DISREGARDED, EnrollmentStage.SERVICE_ACTIVE,
            EnrollmentStage.ON_HOLD, EnrollmentStage.SERVICE_COMPLETE,
        ]
        existing = (
            EnrollmentVerification.objects.filter(client=client)
            .exclude(stage__in=[s.value for s in _TERMINAL_OR_SERVING])
            .order_by("-opened_at").first()
        )
        if existing is not None:
            if hid:
                existing.household = Household.objects.filter(pk=hid).first()
            if case is not None:
                existing.case = case
            if aid:
                existing.delivery_address = Address.objects.filter(pk=aid).first()
            for field, value in validated_data.items():
                setattr(existing, field, value)
            existing.save()
            if members:
                self._sync_members(existing, members)
            return existing

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
# Map an ActiveProgram.case_category value to a Case.case_type. Keys are
# casefolded; both singular/plural spellings from the source data are accepted.
_CATEGORY_TO_CASE_TYPE = {
    # "Navigation" was renamed to "Care Management" (display only -- both map to
    # the same CaseType, whose stored value is still "navigation"). Accept both
    # so legacy + renamed ActiveProgram rows classify identically.
    "navigation": CaseType.NAVIGATION,
    "care management": CaseType.NAVIGATION,
    "eligibility": CaseType.ELIGIBILITY,
    "internal service": CaseType.INTERNAL_SERVICE,
    "internal services": CaseType.INTERNAL_SERVICE,
    # Reauthorization renews an existing meal/box authorization, so it IS an
    # internal-service case: it must drive service the same way (governing-case
    # selection, enrollment, delivery, the program tab) rather than being a
    # separate Navigation case.
    "reauthorization": CaseType.INTERNAL_SERVICE,
    "external service": CaseType.EXTERNAL_SERVICE,
    "external services": CaseType.EXTERNAL_SERVICE,
}


def derive_case_type_from_active_program(program_name):
    """Classify a case by matching its program_name against the ActiveProgram
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
    row = ActiveProgram.objects.filter(program_name__iexact=pn).first()
    if row is None:
        return None
    return _CATEGORY_TO_CASE_TYPE.get((row.case_category or "").strip().casefold())


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
    Otherwise the type comes from the ``program_name``'s ActiveProgram
    category (Eligibility / Navigation / External Service); a blank or unmatched
    program is Navigation.

    Returns None only when there's nothing to classify on (no internal subtype,
    no program match, and no service_type), so callers leave the existing value
    / model default untouched.
    """
    st = (service_type or "").strip()
    if st.casefold() in INTERNAL_SERVICE_SUBTYPES:
        return CaseType.INTERNAL_SERVICE
    from_program = derive_case_type_from_active_program(program_name)
    if from_program is not None:
        return from_program
    if not st:
        return None
    return CaseType.NAVIGATION


# ActiveProgram.case_category values (casefolded) that ARE in scope to import.
# A case is imported only when it is one of our meal/box services OR its program
# belongs to one of these categories. External Services, "Other", a blank/unknown
# program, or a program not present in ActiveProgram are OUT of scope and dropped.
_IN_SCOPE_CASE_CATEGORIES = frozenset({
    "internal service", "internal services",
    "eligibility",
    "reauthorization",
    "care management", "navigation",
    "screening",
})


def active_program_category(program_name):
    """The ActiveProgram ``case_category`` (casefolded) for ``program_name``, or
    None when the program is blank or not in the ActiveProgram table."""
    pn = (program_name or "").strip()
    if not pn:
        return None
    row = ActiveProgram.objects.filter(program_name__iexact=pn).first()
    if row is None:
        return None
    return (row.case_category or "").strip().casefold()


def case_in_import_scope(service_type, program_name=None):
    """True when a case should be imported at all.

    Scope = our meal/box service (by subtype) OR a program that exists in the
    ActiveProgram table AND whose category is one we track (Internal Service,
    Eligibility, Reauthorization, Care Management, Screening). Everything else --
    External Services, "Other", a blank program, or a program NOT in the table --
    is out of scope and must be skipped by the importer.
    """
    if (service_type or "").strip().casefold() in INTERNAL_SERVICE_SUBTYPES:
        return True
    return active_program_category(program_name) in _IN_SCOPE_CASE_CATEGORIES


def derive_is_extension(program_name):
    """True when a case's ``program_name`` matches an ActiveProgram flagged
    ``to_extend`` -- i.e. a reauthorization / service-extension program.

    Case-insensitive, whitespace-trimmed match (mirrors
    ``derive_case_type_from_active_program``). False when the program is blank,
    unmatched, or not flagged to extend.
    """
    pn = (program_name or "").strip()
    if not pn:
        return False
    return ActiveProgram.objects.filter(
        program_name__iexact=pn, to_extend=True
    ).exists()


def derive_household_type(client, program_name=None):
    """Individual vs Household for a case, from the PROGRAM NAME only.

    A household case when the word "Household" appears anywhere in the program
    name (a Met Council "(Household)" eligibility pathway, e.g. "MTM -
    (Household) High-Risk Children Under 18 - Brooklyn"); otherwise a single
    member case.

    The client's own household data (``is_a_family`` / ``household_size``) is
    deliberately NOT considered: it was a frequent source of misclassification
    (a single-member case flipped to Household purely because the client's
    profile reported >1 member). ``client`` is kept in the signature for
    backward-compatibility with existing callers but is unused."""
    is_household = "household" in (program_name or "").casefold()
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
    # Accept a RAW Unite Us authorization state (e.g. "requested", "deferred",
    # "accepted", "rejected") in addition to our own enum values. Declared as a
    # plain CharField to bypass the auto ChoiceField's strict validation so
    # ``to_internal_value`` can normalize it -- mirroring the CSV / daily-import
    # mappers, which pre-map via ``_AUTH_STATE_MAP`` before hitting this
    # serializer. Without this, an extension save that sent a raw/denied state
    # never updated the stored enum (the bug: a rejected auth still read
    # "requested"/pending).
    service_authorization_status = serializers.CharField(
        required=False, allow_blank=True
    )

    class Meta:
        model = Case
        exclude = (
            "client",
            "previous_case",
            "originating_provider",
            "provider",
            "program",
        )

    def to_internal_value(self, data):
        ret = super().to_internal_value(data)
        # Normalize the authorization status so EVERY write path (extension,
        # import, admin) persists the same enum. The extension writes straight
        # through this serializer with no mapping, so a raw Unite Us state
        # ("rejected"/"requested"/"accepted"/"deferred") must be translated here
        # or the stored enum never updates. Mirrors mappers._AUTH_STATE_MAP.
        raw = data.get("service_authorization_status") if hasattr(data, "get") else None
        if raw not in (None, ""):
            from api.integrations.uniteus.mappers import (
                _AUTH_STATE_MAP,
                _enum_or_blank,
            )

            ret["service_authorization_status"] = _enum_or_blank(
                raw, ServiceAuthorizationStatus.values, _AUTH_STATE_MAP
            )
            # Preserve the human-readable raw label when the caller didn't send
            # one, so the UI keeps fidelity (e.g. "Rejected").
            if not (data.get("service_authorization_status_label") or "").strip():
                ret["service_authorization_status_label"] = (
                    str(raw).replace("_", " ").title()
                )
        return ret

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

        # Extension guard: a case logged through the browser extension must be
        # MANAGED by Met Council. Reject one attributed to another organization
        # -- OR one with NO managing organization at all (blank provider) --
        # since those are out of scope and must never enter the member base from
        # an agent write. Only enforced for extension/CRM HTTP writes (the
        # request principal is an authenticated Agent, i.e. carries ``agent_id``
        # -- NOT ``agent_code``, which is null for agents without a dialer
        # extension and would let their writes slip past this gate); the CSV
        # import + nightly Unite Us pull build their payloads with no request in
        # context and apply their own gate first (and legitimately keep blank-org
        # internal-service meal cases), so they're unaffected.
        request = self.context.get("request")
        request_user = getattr(request, "user", None) if request is not None else None
        if getattr(request_user, "agent_id", None):
            from api.services.lifecycle import is_met_council_case

            if not is_met_council_case(
                provider_id=provider_id,
                provider_name=validated_data.get("provider_name"),
                allow_originating=False,
            ):
                raise serializers.ValidationError(
                    {"provider_name": (
                        "This case isn't managed by Met Council (or has no "
                        "managing organization), so it can't be added."
                    )}
                )

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
            validated_data["household_type"] = derive_household_type(
                client, validated_data.get("program_name")
            )
        # Reauthorization / extension classification (drives scheduled-extension
        # governing-case handling). An explicit payload value wins.
        if "is_extension" not in validated_data:
            validated_data["is_extension"] = derive_is_extension(
                validated_data.get("program_name")
            )

        # External Service cases are out of scope -- we never track them. Reject
        # the write outright (whether the type was set explicitly or derived from
        # the program). This is the universal backstop: import paths pre-skip
        # external-service rows, so in practice this only rejects a direct /
        # extension / admin save.
        if validated_data.get("case_type") == CaseType.EXTERNAL_SERVICE:
            raise serializers.ValidationError(
                {"case_type": "External Service cases are not tracked and cannot be saved."}
            )

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

        # Case status is Open/Closed ONLY, driven by the closed date (mirrors the
        # extension + the CSV/API mappers). Authorization status is an INDEPENDENT
        # dimension and no longer forces the case status -- a denied authorization
        # leaves the case Open and instead pauses the household via the
        # internal-service reconcile below. (The old "denied -> Closed" coupling
        # was removed so authorization never drives case status.)
        #
        # A populated close date is the authoritative "closed" signal, enforced
        # HERE -- the central chokepoint for every write path -- not just in the
        # CSV/API mappers. Unite Us keeps a closed case as "managed" in its data,
        # so any save carrying a ``case_closed_at`` must land as CLOSED regardless
        # of the raw incoming ``case_status``. Without this the extension (which
        # passes the Unite Us state straight through) persisted closed cases as
        # "Managed". We only FORCE the closed direction: a write with no close
        # date keeps whatever status it carried (authorization stays independent
        # of case status -- see AuthDrivesCaseStatusTest).
        if validated_data.get("case_closed_at"):
            validated_data["case_status"] = CaseStatus.CLOSED

        case, _ = Case.objects.update_or_create(case_id=case_id, defaults=validated_data)
        # Stash the pre-save values on the instance so the write path (e.g.
        # CaseViewSet, extension) can record the change + attribute it, without
        # re-querying. Import/daily-sync paths capture prev themselves.
        case._prev_status = _prev_status
        case._prev_auth = _prev_auth
        # Extension attribution: stamp the AUTHENTICATED agent as the case
        # creator the FIRST time a case is saved (``_prev is None``). Unite Us
        # imports carry their own source ``created_by`` in the payload, so the
        # "only when blank" guard preserves it; Django-admin / CRM writes have no
        # ``agent_code`` and are skipped. This is what fills the Urgent Care
        # "Created By" column for cases logged through the extension.
        try:
            request = self.context.get("request")
            agent = getattr(request, "user", None) if request is not None else None
            if (
                _prev is None
                and getattr(agent, "agent_code", None)
                and not case.created_by_name
            ):
                stamped = []
                if getattr(agent, "name", None):
                    case.created_by_name = agent.name
                    stamped.append("created_by_name")
                if getattr(agent, "agent_id", None) and case.created_by_id is None:
                    case.created_by_id = agent.agent_id
                    stamped.append("created_by_id")
                if stamped:
                    case.save(update_fields=stamped)
        except Exception:
            logger.exception("agent created_by stamp failed for case %s", case_id)
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
        # meal/box delivery, so ensure the client is anchored as the PRIMARY of
        # their own household (and their enrollments re-homed there) on case save.
        #
        # BUT: a client who was added as a relative's DEPENDENT (a non-primary
        # household member) is NOT split out here anymore. The split into their
        # own case/household now happens only when an agent Requests Verification
        # for them (split_dependent_into_own_enrollment) -- so they stay visible
        # in the shared household until then, and the "dependent in a household"
        # warning on the case-save response can actually surface (the eager split
        # used to remove their membership before that warning was evaluated).
        # For a primary / household-less client this still heals mis-anchored
        # enrollments. Idempotent.
        try:
            if case.case_type == CaseType.INTERNAL_SERVICE:
                _membership = HouseholdMember.objects.filter(client=client).first()
                _is_dependent = _membership is not None and not _membership.is_primary
                if not _is_dependent:
                    ensure_primary_of_own_household(client)
        except Exception:
            logger.exception("ensure_primary_of_own_household failed for internal service case")
        # "New client needs verification attention": creating a client's FIRST
        # internal-service case flags them is_new=True so they surface on the
        # Urgent Care ("Need Attention") list and the ext shows the right
        # screening warning. Fires for the two legitimate ingestion sources:
        #   * EXTENSION -- the request user carries an ``agent_code`` (an
        #     AgentUser from the agent JWT, see api.authentication).
        #   * IMPORT    -- the CSV import / nightly Unite Us sync run inside a
        #     ``change_context(ChangeSource.IMPORT, ...)`` block (case imports are
        #     now Met Council-only, so only our own cases reach here).
        # Django-admin / CRM writes are excluded (no agent_code, no IMPORT
        # context). Requires the case to be newly created (``_prev`` is None) and
        # the client to meet the FULL Urgent Care gate (open internal-service
        # case, no verification requested, valid Medicaid + social care) --
        # ``evaluate_is_new_flag`` enforces all of that. For the CSV/Unite Us
        # import, clients (with their insurance + coverage) are loaded before
        # their cases, so the gate can see the coverage here; anything the import
        # misses is caught by the ``review_urgent_care_candidates`` command.
        # Cleared once a verification completes (advance_enrollment -> VERIFIED).
        # Best-effort: never let the flag break the case save.
        try:
            from api.history import ChangeSource, current_change_source
            from api.services.lifecycle import evaluate_is_new_flag

            request = self.context.get("request")
            request_user = getattr(request, "user", None) if request is not None else None
            is_ext_write = bool(getattr(request_user, "agent_code", None))
            is_import_write = current_change_source() == ChangeSource.IMPORT
            if (
                (is_ext_write or is_import_write)
                and case.case_type == CaseType.INTERNAL_SERVICE
                and _prev is None
            ):
                evaluate_is_new_flag(client)
        except Exception:
            logger.exception("is_new flag set failed for internal service case %s", case_id)
        # Internal-service case-driven lifecycle reconcile (the single
        # chokepoint for ALL save paths -- extension, CSV import, bulk CLI):
        #   * favorable governing authorization -> advance a verified household to
        #     Kitchen Assignment / resume an auto-paused hold;
        #   * denied governing authorization -> pause the household (On Hold);
        #   * the last open internal-service case closing -> pause + truncate
        #     deliveries + note the primary, then cancel + a second note.
        # Opens NO tickets: visibility comes from StageEvents, the member
        # timeline, the primary notes, and the Import Activity page. Attributed to
        # the acting agent when present. Best-effort: never break the case save.
        try:
            if case.case_type == CaseType.INTERNAL_SERVICE:
                from .services.lifecycle import (
                    internal_service_reconcile_deferred,
                    reconcile_internal_service_authorization,
                )

                # Imports defer this: they save one case per row, so reconciling
                # here would evaluate the client-wide rules against a partial
                # picture (e.g. cancel a household before the row for its still-
                # open case is written). The import runs the reconcile ONCE per
                # client on the full picture after all rows land. Single-case
                # writes (extension/portal) are NOT deferred -> reconcile now.
                if not internal_service_reconcile_deferred():
                    request = self.context.get("request")
                    agent = getattr(request, "user", None) if request is not None else None
                    reconcile_internal_service_authorization(client, actor=agent)
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
