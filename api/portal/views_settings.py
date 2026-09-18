"""Settings CRUD: menu types, dietary tags, kitchens, delivery companies and
their integrations."""

import logging
from django.shortcuts import get_object_or_404
from rest_framework import status as http, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import (
    Vendor,
    VendorUser,
    ActiveProgram,
    Agent,
    Cadence,
    ClientTag,
    DeliveryCompany,
    DeliveryCompanyIntegration,
    DietaryTag,
    DietaryTagType,
    Kitchen,
    KitchenIntegration,
    KitchenMenuType,
    MealPlan,
    MenuType,
    MenuTypeTag,
    ProductType,
    ProgramMainCategory,
)
from .base import PortalAPIView, current_agent

logger = logging.getLogger(__name__)
from .permissions import IsPortalAgent
from . import serializers as s


def _clean_config(method, config, existing=None):
    """Persisted integration config. A masked apiKey ('********') means 'keep the
    existing key', so we never overwrite a real secret with the mask."""
    cfg = dict(config or {})
    if cfg.get("apiKey") == "********":
        cfg["apiKey"] = (existing or {}).get("apiKey", "")
    return cfg


class MenuTypeViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = MenuType.objects.all().prefetch_related("tags")
    serializer_class = s.PortalMenuTypeSerializer

    @action(detail=True, methods=["put"])
    def tags(self, request, pk=None):
        """Replace the menu type's dietary tags. Body: {"tag_ids": [...]}."""
        menu = self.get_object()
        tag_ids = request.data.get("tag_ids", [])
        MenuTypeTag.objects.filter(menu_type=menu).delete()
        for tid in tag_ids:
            tag = DietaryTag.objects.filter(pk=tid).first()
            if tag:
                MenuTypeTag.objects.get_or_create(menu_type=menu, dietary_tag=tag)
        menu.refresh_from_db()
        return Response(self.get_serializer(menu).data)


class MealPlanViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = MealPlan.objects.all()
    serializer_class = s.PortalMealPlanSerializer


class DietaryTagViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = DietaryTag.objects.all()
    serializer_class = s.PortalDietaryTagSerializer

    def destroy(self, request, *args, **kwargs):
        tag = self.get_object()
        if tag.menu_type_tags.exists():
            return Response(
                {"error": "Remove this tag from all menu types before deleting it."},
                status=http.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class ClientTagViewSet(viewsets.ModelViewSet):
    """Settings > Tags: manage colour-coded client labels (name + colour)."""

    permission_classes = [IsPortalAgent]
    queryset = ClientTag.objects.all()
    serializer_class = s.PortalClientTagSerializer


class CadenceViewSet(viewsets.ModelViewSet):
    """Settings > Delivery Cadences: manage the configurable delivery cadences
    (label + delivery weekdays). Configuration only for now -- surfaced in
    Settings and selectable per-kitchen; the scheduling core still reads the
    legacy enum/weekday map."""

    permission_classes = [IsPortalAgent]
    queryset = Cadence.objects.all()
    serializer_class = s.PortalCadenceSerializer
    pagination_class = None

    def destroy(self, request, *args, **kwargs):
        cadence = self.get_object()
        # Don't remove a cadence a ProductType is still built on -- deleting it
        # would orphan the option a live schedule depends on.
        if ProductType.objects.filter(delivery_days_cadence=cadence.code).exists():
            return Response(
                {"error": "A product type still uses this cadence. Reassign it before deleting."},
                status=http.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class KitchenViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = Kitchen.objects.all().prefetch_related(
        "menu_types",
        "cadences",
        "integrations",
        "kitchen_menu_types__menu_type",
        "kitchen_menu_types__restrictions",
    )
    serializer_class = s.PortalKitchenSerializer

    @action(detail=True, methods=["put"], url_path="menu-types")
    def menu_types(self, request, pk=None):
        kitchen = self.get_object()
        kitchen.menu_types.set(request.data.get("menu_type_ids", []))
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["put"])
    def cadences(self, request, pk=None):
        """Set which delivery cadences this kitchen takes orders for.
        Body: {"cadence_ids": [...]}. Configuration only (not yet enforced)."""
        kitchen = self.get_object()
        kitchen.cadences.set(request.data.get("cadence_ids", []))
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["put"], url_path="menu-type-config")
    def menu_type_config(self, request, pk=None):
        """Set the per-kitchen price and unmanageable allergies for ONE offered
        menu type. Body: {menu_type_id, price?, restriction_tag_ids?}.

        Only DietaryTags of type ``allergy`` are accepted as restrictions."""
        kitchen = self.get_object()
        mt_id = request.data.get("menu_type_id")
        kmt = (
            KitchenMenuType.objects.filter(kitchen=kitchen, menu_type_id=mt_id)
            .first()
        )
        if kmt is None:
            return Response(
                {"error": "This kitchen does not offer that menu type."},
                status=http.HTTP_404_NOT_FOUND,
            )
        if "price" in request.data:
            kmt.menu_type_price = request.data.get("price") or None
            kmt.save(update_fields=["menu_type_price"])
        if "restriction_tag_ids" in request.data:
            tags = DietaryTag.objects.filter(
                pk__in=request.data.get("restriction_tag_ids") or [],
                type=DietaryTagType.ALLERGY,
            )
            kmt.restrictions.set(tags)
        kitchen.refresh_from_db()
        return Response(self.get_serializer(kitchen).data)

    @action(detail=True, methods=["post"])
    def integrations(self, request, pk=None):
        """Add an integration. Kitchen integrations are unique per method, so a
        second integration of the same method is rejected (no is_primary)."""
        kitchen = self.get_object()
        method = request.data.get("method")
        if method not in ("email", "api"):
            return Response({"error": "method must be 'email' or 'api'."}, status=http.HTTP_400_BAD_REQUEST)
        if kitchen.integrations.filter(method=method).exists():
            return Response(
                {"error": f"This kitchen already has a {method} integration."},
                status=http.HTTP_409_CONFLICT,
            )
        integ = KitchenIntegration.objects.create(
            kitchen=kitchen, method=method, config=_clean_config(method, request.data.get("config")),
        )
        return Response(
            s.PortalKitchenIntegrationSerializer(integ).data, status=http.HTTP_201_CREATED
        )


class KitchenIntegrationDetailView(PortalAPIView):
    def patch(self, request, integration_id):
        integ = get_object_or_404(KitchenIntegration, pk=integration_id)
        if "config" in request.data:
            integ.config = _clean_config(integ.method, request.data["config"], integ.config)
        integ.save()
        return Response(s.PortalKitchenIntegrationSerializer(integ).data)

    def delete(self, request, integration_id):
        get_object_or_404(KitchenIntegration, pk=integration_id).delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class DeliveryCompanyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPortalAgent]
    queryset = DeliveryCompany.objects.all().prefetch_related("integrations")
    serializer_class = s.PortalDeliveryCompanySerializer

    @action(detail=True, methods=["post"])
    def integrations(self, request, pk=None):
        company = self.get_object()
        method = request.data.get("method")
        # Delivery companies no longer RECEIVE orders from us by email -- the
        # outbound integration was never wired up (zero rows in production) and
        # has been retired. The only integration is now INBOUND: they push proof
        # of delivery to the partner API, whose credential lives on
        # DeliveryCompanyApiClient (see views_partner_credentials). Kitchens keep
        # their own email/api integration; that is a separate model.
        if method != "api":
            return Response(
                {"error": "method must be 'api'. Email delivery integrations have "
                          "been retired; use POD API access instead."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if company.integrations.filter(method=method).exists():
            return Response(
                {"error": f"This company already has a {method} integration."},
                status=http.HTTP_409_CONFLICT,
            )
        is_primary = bool(request.data.get("is_primary")) or not company.integrations.exists()
        if is_primary:
            company.integrations.update(is_primary=False)
        integ = DeliveryCompanyIntegration.objects.create(
            delivery_company=company,
            method=method,
            is_primary=is_primary,
            config=_clean_config(method, request.data.get("config")),
        )
        return Response(
            s.PortalDeliveryCompanyIntegrationSerializer(integ).data,
            status=http.HTTP_201_CREATED,
        )


class DeliveryCompanyIntegrationDetailView(PortalAPIView):
    def patch(self, request, integration_id):
        integ = get_object_or_404(DeliveryCompanyIntegration, pk=integration_id)
        if "config" in request.data:
            integ.config = _clean_config(integ.method, request.data["config"], integ.config)
        integ.save()
        return Response(s.PortalDeliveryCompanyIntegrationSerializer(integ).data)

    def delete(self, request, integration_id):
        get_object_or_404(DeliveryCompanyIntegration, pk=integration_id).delete()
        return Response(status=http.HTTP_204_NO_CONTENT)


class DeliveryCompanyIntegrationSetPrimaryView(PortalAPIView):
    def post(self, request, integration_id):
        integ = get_object_or_404(DeliveryCompanyIntegration, pk=integration_id)
        integ.delivery_company.integrations.update(is_primary=False)
        integ.is_primary = True
        integ.save(update_fields=["is_primary"])
        return Response(s.PortalDeliveryCompanyIntegrationSerializer(integ).data)


class CrmAgentViewSet(viewsets.ModelViewSet):
    """Settings > CareCircle Agents: manage our internal CRM agent roster.

    Full list (no pagination, so the UI can search/filter client-side) plus
    create/update/delete. Optional ``?search=`` (name/email/code) and ``?group=``
    query filters.

    Deleting is allowed, but deactivating via ``status`` is preferred for agents
    with history: every reference to an agent (enrollment verified_by/requested_by,
    ticket created_by/assigned_to, lead assigned_to, report exports) is a
    ``SET_NULL`` FK, so a delete never cascades -- it just detaches those rows,
    which loses the agent attribution on that history. Tickets keep their
    ``created_by_label`` snapshot regardless.
    """

    permission_classes = [IsPortalAgent]
    serializer_class = s.PortalCrmAgentSerializer
    pagination_class = None
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def get_queryset(self):
        qs = Agent.objects.all().order_by("name")
        params = self.request.query_params
        group = (params.get("group") or "").strip()
        if group:
            qs = qs.filter(group=group)
        search = (params.get("search") or "").strip()
        if search:
            from django.db.models import Q

            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(agent_code__icontains=search)
                | Q(title__icontains=search)
            )
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        data = self.get_serializer(qs, many=True).data
        # Surface the selectable group choices so the UI dropdown stays in sync
        # with the model without hard-coding them on the frontend.
        return Response(
            {
                "count": len(data),
                "groups": [g[0] for g in Agent.AGENT_GROUPS],
                "results": data,
            }
        )


class ProgramMainCategoryViewSet(viewsets.ModelViewSet):
    """Settings > Program Categories: edit / activate / delete the program
    main-category master list.

    Categories are built up from Screening results, so there is NO create. They
    are opt-in: inactive by default, an admin activates the ones this org
    actually serves. Full list (no pagination for client-side search) with
    optional ``?search=`` (name) and ``?active=true|false``. Each row carries a
    read-only ``program_count`` (programs linked to the category).
    """

    permission_classes = [IsPortalAgent]
    serializer_class = s.PortalProgramMainCategorySerializer
    pagination_class = None
    http_method_names = ["get", "patch", "put", "delete", "head", "options"]

    def get_queryset(self):
        from django.db.models import Count

        qs = ProgramMainCategory.objects.annotate(program_count=Count("programs"))
        params = self.request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        active = (params.get("active") or "").strip().lower()
        if active in ("true", "1"):
            qs = qs.filter(is_active=True)
        elif active in ("false", "0"):
            qs = qs.filter(is_active=False)
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        data = self.get_serializer(qs, many=True).data
        return Response(
            {
                "count": len(data),
                "active_count": sum(1 for c in data if c["is_active"]),
                "results": data,
            }
        )


class ActiveProgramViewSet(viewsets.ModelViewSet):
    """Settings > Programs: manage the ActiveProgram classification table -- the
    Program Name -> Case Category map that decides, on import, whether a case is
    Internal / External Service, Eligibility or Care Management (see
    ``api.serializers.derive_case_type_from_active_program``).

    Agents can add / edit / delete rows and set each program's ``case_category``
    and ``case_type`` (Food/Transportation). ``is_for_household`` is auto-derived
    from the name on save. Full list (no pagination for client-side search) with
    optional ``?search=`` (program name), ``?category=`` (case_category),
    ``?case_type=food|transportation`` and ``?service_type=<code>|none``.
    """

    permission_classes = [IsPortalAgent]
    serializer_class = s.PortalActiveProgramSerializer
    pagination_class = None
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def get_queryset(self):
        qs = ActiveProgram.objects.all()
        params = self.request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(program_name__icontains=search)
        category = (params.get("category") or "").strip()
        if category:
            qs = qs.filter(case_category__iexact=category)
        case_type = (params.get("case_type") or "").strip().lower()
        if case_type in ActiveProgram.CaseType.values:
            qs = qs.filter(case_type=case_type)
        # Service type: a valid code, or "none" for the programs with none set
        # (so the unclassified ones are reachable from the UI too).
        service_type = (params.get("service_type") or "").strip().lower()
        if service_type == "none":
            qs = qs.filter(service_type="")
        elif service_type in ActiveProgram.ServiceType.values:
            qs = qs.filter(service_type=service_type)
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        data = self.get_serializer(qs, many=True).data
        # Distinct case categories currently in use, so the UI dropdown stays in
        # sync with the data without hard-coding the (free-text) labels.
        categories = list(
            ActiveProgram.objects.exclude(case_category="")
            .order_by("case_category")
            .values_list("case_category", flat=True)
            .distinct()
        )
        return Response(
            {
                "count": len(data),
                "categories": categories,
                "case_types": [
                    {"value": v, "label": label}
                    for v, label in ActiveProgram.CaseType.choices
                ],
                # Service the program delivers; blank ("—") is a valid choice for
                # programs that aren't one of the services we deliver.
                "service_types": [
                    {"value": v, "label": label}
                    for v, label in ActiveProgram.ServiceType.choices
                ],
                "results": data,
            }
        )


class VendorViewSet(viewsets.ModelViewSet):
    """Settings > Vendors -- housing vendor companies and their users.

    Vendors are provisioned here: create the company, then ONE admin user. That
    person creates the rest of their staff in the vendor portal; the CRM keeps only
    the ability to reset THIS user's password.

    See docs/housing-assessment-order-plan.md.
    """

    permission_classes = [IsPortalAgent]
    queryset = Vendor.objects.all().prefetch_related("users", "dispatch_orders")
    serializer_class = s.PortalVendorSerializer

    def perform_update(self, serializer):
        """Log an activation change; it is not just another field edit.

        Deactivating a vendor removes them from every assignment dropdown, so work
        can no longer be sent their way. Worth knowing who did it and whether they
        still had live orders at the time.
        """
        was_active = serializer.instance.is_active
        vendor = serializer.save()
        if vendor.is_active != was_active:
            open_orders = self._open_order_count(vendor)
            _log_vendor_action(
                self.request, vendor,
                "activated" if vendor.is_active else "deactivated",
                {"open_orders": open_orders},
            )

    @staticmethod
    def _open_order_count(vendor):
        from ..models import DispatchStatus

        return vendor.dispatch_orders.exclude(
            status__in=[DispatchStatus.UPLOADED, DispatchStatus.CANCELLED],
        ).count()

    def destroy(self, request, *args, **kwargs):
        """Refuse to DELETE a vendor that holds orders; deactivate instead.

        DispatchOrder.vendor is PROTECT, so the delete would fail at the database
        with an opaque error. More importantly, a vendor is history: deleting one
        would erase who did the work on every order they ever executed.
        """
        vendor = self.get_object()
        if vendor.dispatch_orders.exists():
            return Response(
                {
                    "error": (
                        "This vendor has orders and cannot be deleted — the record "
                        "of who did that work must survive. Deactivate them instead."
                    )
                },
                status=http.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="admin-user")
    def admin_user(self, request, pk=None):
        """Provision the vendor's single admin user.

        Refuses a second: the model constrains it, and returning 409 with the
        existing user is more useful than surfacing an IntegrityError.
        """
        vendor = self.get_object()
        existing = vendor.users.filter(is_admin=True).first()
        if existing is not None:
            return Response(
                {
                    "error": "This vendor already has an admin user.",
                    "admin_user": s.PortalVendorUserSerializer(existing).data,
                },
                status=http.HTTP_409_CONFLICT,
            )

        email = (request.data.get("email") or "").strip().lower()
        name = (request.data.get("name") or "").strip()
        if not email or not name:
            return Response(
                {"error": "email and name are required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if VendorUser.objects.filter(email__iexact=email).exists():
            return Response(
                {"error": "A vendor user with that email already exists."},
                status=http.HTTP_409_CONFLICT,
            )

        from django.contrib.auth.hashers import make_password
        from django.utils.crypto import get_random_string

        # An agent may SET the password (they are handing it over by phone or in
        # person); otherwise one is generated. Either way it is stored hashed and
        # returned ONCE -- there is no path that reads it back, and a lost password
        # is replaced by a reset rather than looked up.
        supplied = (request.data.get("password") or "").strip()
        if supplied and len(supplied) < 8:
            return Response(
                {"error": "Password must be at least 8 characters."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        temp_password = supplied or get_random_string(14)
        user = VendorUser.objects.create(
            vendor=vendor, email=email, name=name,
            phone=(request.data.get("phone") or "").strip(),
            password=make_password(temp_password),
            is_admin=True,
        )
        _log_vendor_action(
            request, vendor, "admin user provisioned", {"email": email},
        )
        payload = s.PortalVendorUserSerializer(user).data
        # Echoed only when WE generated it. An agent who typed the password already
        # has it, and repeating it back puts a secret they chose into a response
        # body and any log that captures one.
        payload["temporary_password"] = "" if supplied else temp_password
        payload["password_was_supplied"] = bool(supplied)
        return Response(payload, status=http.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reset-admin-password")
    def reset_admin_password(self, request, pk=None):
        """Reset the vendor admin's password.

        An agent may SUPPLY one -- they are usually reading it down the phone --
        or leave it blank to have one generated. Same rule as provisioning, which
        is the point: being able to type a password when creating an account but
        not when resetting it is a surprise with no reason behind it.

        A privileged action on an EXTERNAL account, so it is recorded: who reset
        it and when.
        """
        vendor = self.get_object()
        admin = vendor.users.filter(is_admin=True).first()
        if admin is None:
            return Response(
                {"error": "This vendor has no admin user yet."},
                status=http.HTTP_404_NOT_FOUND,
            )

        from django.contrib.auth.hashers import make_password
        from django.utils.crypto import get_random_string

        supplied = (request.data.get("password") or "").strip()
        if supplied and len(supplied) < 8:
            return Response(
                {"error": "Password must be at least 8 characters."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        temp_password = supplied or get_random_string(14)
        admin.password = make_password(temp_password)
        admin.save(update_fields=["password", "updated_at"])
        _log_vendor_action(
            request, vendor, "admin password reset", {
                "email": admin.email,
                # Whether it was chosen or generated, never the value itself.
                "supplied": bool(supplied),
            },
        )
        return Response({
            "vendor_user_id": str(admin.vendor_user_id),
            "email": admin.email,
            # Echoed only when WE generated it. An agent who typed the password
            # already has it, and repeating it back puts a secret they chose into
            # a response body and any log that captures one.
            "temporary_password": "" if supplied else temp_password,
            "password_was_supplied": bool(supplied),
        })


def _log_vendor_action(request, vendor, what, extra=None):
    """Record a privileged action against an external vendor account.

    Vendor provisioning and password resets happen to accounts we do not own, so
    "who did this and when" needs to survive the request. Written to the
    application log rather than a new table: the volume is tiny and CloudWatch
    already ships these.
    """
    agent = current_agent(request)
    logger.warning(
        "vendor action: %s | vendor=%s (%s) | agent=%s (%s) | %s",
        what, vendor.name, vendor.pk,
        getattr(agent, "name", "?"), getattr(agent, "agent_code", "?"),
        extra or {},
    )


class BillableItemViewSet(viewsets.ModelViewSet):
    """Settings > Pricing: the housing price list.

    ``vendor_price`` is what we RECOMMEND a vendor charge us; the billed price is
    that plus the adjustable admin fee, and is DERIVED on every read rather than
    stored -- so changing the fee reprices the whole list at once, which is what
    makes it adjustable in any useful sense.

    Per-vendor pricing comes later. This is the base every vendor starts from.
    """

    serializer_class = s.BillableItemSerializer
    permission_classes = [IsPortalAgent]

    def get_queryset(self):
        from ..models import BillableItem

        qs = BillableItem.objects.all()
        if (self.request.query_params.get("active_only") or "").lower() in (
            "1", "true", "yes",
        ):
            qs = qs.filter(is_active=True)
        return qs

    def perform_update(self, serializer):
        from ..models import BillableItem

        before = BillableItem.objects.get(pk=serializer.instance.pk)
        item = serializer.save()
        # A price change is a money change, so it is recorded with the old value.
        # "What did we charge before?" is the first question anyone asks about a
        # disputed invoice.
        if before.vendor_price != item.vendor_price:
            agent = current_agent(self.request)
            logger.warning(
                "pricing: %s changed %s from %s to %s | agent=%s",
                "agent", item.item, before.vendor_price, item.vendor_price,
                getattr(agent, "name", "?"),
            )

    @action(detail=False, methods=["get", "patch"], url_path="billing-settings")
    def billing_settings(self, request):
        """The admin fee, as a percentage. One row, so no id in the path."""
        from decimal import Decimal, InvalidOperation

        from ..models import BillingSettings

        settings_row = BillingSettings.get()
        if request.method.lower() == "patch":
            raw = request.data.get("admin_fee_percent")
            try:
                pct = Decimal(str(raw))
            except (InvalidOperation, TypeError):
                return Response(
                    {"error": "admin_fee_percent must be a number."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            # 0 is legitimate -- billing at cost. Above 100 is not: it would mean
            # charging more than double, which is a typo rather than a policy.
            if pct < 0 or pct > 100:
                return Response(
                    {"error": "The admin fee must be between 0 and 100 percent."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            if settings_row.admin_fee_percent != pct:
                logger.warning(
                    "pricing: admin fee changed from %s%% to %s%% | agent=%s",
                    settings_row.admin_fee_percent, pct,
                    getattr(current_agent(request), "name", "?"),
                )
                settings_row.admin_fee_percent = pct
                settings_row.updated_by = current_agent(request) if isinstance(
                    current_agent(request), Agent,
                ) else None
                settings_row.save()
        return Response({
            "admin_fee_percent": str(settings_row.admin_fee_percent),
            "updated_at": settings_row.updated_at,
        })
