from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import viewsets

from common.permissions import IsAdminOrReadOnly

from .cache import CatalogCacheMixin
from .models import CentreTest, DiagnosticCentre, DiagnosticTest
from .serializers import CentreTestSerializer, DiagnosticCentreSerializer, DiagnosticTestSerializer


class SoftDeleteMixin:
    """DELETE deactivates instead of removing rows that bookings may reference."""

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


@extend_schema_view(
    list=extend_schema(
        summary="List diagnostic centres",
        parameters=[
            OpenApiParameter("city", OpenApiTypes.STR, description="Filter by city (case-insensitive)"),
            OpenApiParameter("test", OpenApiTypes.STR, description="Only centres offering this test code, e.g. CBC"),
            OpenApiParameter("search", OpenApiTypes.STR, description="Search in centre name / address"),
        ],
    ),
    retrieve=extend_schema(summary="Get a centre with the tests it offers"),
    create=extend_schema(summary="Create a centre (staff only)"),
    partial_update=extend_schema(summary="Update a centre (staff only)"),
    update=extend_schema(summary="Replace a centre (staff only)"),
    destroy=extend_schema(summary="Deactivate a centre (staff only)"),
)
class DiagnosticCentreViewSet(SoftDeleteMixin, CatalogCacheMixin, viewsets.ModelViewSet):
    serializer_class = DiagnosticCentreSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        qs = DiagnosticCentre.objects.all()
        if not self.request.user.is_staff:
            qs = qs.filter(is_active=True)

        params = self.request.query_params
        if city := params.get("city", "").strip():
            qs = qs.filter(city__iexact=city)
        if test_code := params.get("test", "").strip():
            qs = qs.filter(
                offerings__test__code__iexact=test_code,
                offerings__is_available=True,
                offerings__test__is_active=True,
            )
        if search := params.get("search", "").strip():
            qs = qs.filter(Q(name__icontains=search) | Q(address__icontains=search))

        available = CentreTest.objects.filter(is_available=True, test__is_active=True).select_related("test")
        return qs.distinct().prefetch_related(Prefetch("offerings", queryset=available, to_attr="available_offerings"))


@extend_schema_view(
    list=extend_schema(
        summary="List diagnostic tests in the catalogue",
        parameters=[OpenApiParameter("search", OpenApiTypes.STR, description="Search in test code / name")],
    ),
    retrieve=extend_schema(summary="Get a diagnostic test"),
    create=extend_schema(summary="Create a test (staff only)"),
    partial_update=extend_schema(summary="Update a test (staff only)"),
    update=extend_schema(summary="Replace a test (staff only)"),
    destroy=extend_schema(summary="Deactivate a test (staff only)"),
)
class DiagnosticTestViewSet(SoftDeleteMixin, CatalogCacheMixin, viewsets.ModelViewSet):
    serializer_class = DiagnosticTestSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        qs = DiagnosticTest.objects.all()
        if not self.request.user.is_staff:
            qs = qs.filter(is_active=True)
        if search := self.request.query_params.get("search", "").strip():
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return qs


@extend_schema_view(
    list=extend_schema(summary="List tests (with prices) offered by a centre"),
    retrieve=extend_schema(summary="Get one test offering of a centre"),
    create=extend_schema(summary="Add a test + price to a centre (staff only)"),
    partial_update=extend_schema(summary="Change price / availability (staff only)"),
    update=extend_schema(summary="Replace an offering (staff only)"),
    destroy=extend_schema(summary="Remove a test from a centre (staff only)"),
)
class CentreTestViewSet(CatalogCacheMixin, viewsets.ModelViewSet):
    serializer_class = CentreTestSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_centre(self):
        if not hasattr(self, "_centre"):
            centres = DiagnosticCentre.objects.all()
            if not self.request.user.is_staff:
                centres = centres.filter(is_active=True)
            self._centre = get_object_or_404(centres, pk=self.kwargs["centre_pk"])
        return self._centre

    def get_queryset(self):
        qs = CentreTest.objects.filter(centre=self.get_centre()).select_related("test")
        if not self.request.user.is_staff:
            qs = qs.filter(is_available=True, test__is_active=True)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "centre_pk" in self.kwargs:  # absent during schema generation
            context["centre"] = self.get_centre()
        return context

    def perform_create(self, serializer):
        serializer.save(centre=self.get_centre())
