from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Booking, BookingStatus
from .serializers import BookingCreateSerializer, BookingSerializer
from .services import cancel_booking, create_booking


@extend_schema_view(
    list=extend_schema(
        summary="List my bookings",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, enum=BookingStatus.values)],
    ),
    retrieve=extend_schema(summary="Get one of my bookings"),
)
class BookingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Bookings are created and cancelled through explicit endpoints; there is no
    generic update/delete, so a booking's status can only change through the
    state machine.
    """

    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation, no real user
            return Booking.objects.none()
        qs = Booking.objects.select_related("centre", "test")
        # Patients only ever see their own bookings. Anything else is a 404 (not 403),
        # so we don't leak whether a booking ID exists.
        if not self.request.user.is_staff:
            qs = qs.filter(user=self.request.user)
        if (status_filter := self.request.query_params.get("status", "").upper()) in BookingStatus.values:
            qs = qs.filter(status=status_filter)
        return qs

    @extend_schema(
        summary="Book a diagnostic test", request=BookingCreateSerializer, responses={201: BookingSerializer}
    )
    def create(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = create_booking(
            user=request.user,
            offering=serializer.validated_data["offering"],
            appointment_at=serializer.validated_data["appointment_at"],
        )
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)

    @extend_schema(summary="Cancel a booking", request=None, responses={200: BookingSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        booking = self.get_object()  # enforces ownership
        booking = cancel_booking(booking_id=booking.pk)
        return Response(BookingSerializer(booking).data)
