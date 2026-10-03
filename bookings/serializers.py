from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from catalog.models import CentreTest, DiagnosticCentre, DiagnosticTest

from .models import Booking


class BookingCentreSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiagnosticCentre
        fields = ["id", "name", "city", "address"]


class BookingTestSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiagnosticTest
        fields = ["id", "code", "name"]


class BookingSerializer(serializers.ModelSerializer):
    centre = BookingCentreSerializer(read_only=True)
    test = BookingTestSerializer(read_only=True)
    user_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "user_id",
            "centre",
            "test",
            "appointment_at",
            "amount",
            "currency",
            "status",
            "created_at",
            "updated_at",
            "cancelled_at",
        ]
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    centre_id = serializers.PrimaryKeyRelatedField(
        queryset=DiagnosticCentre.objects.filter(is_active=True),
        error_messages={"does_not_exist": "Diagnostic centre {pk_value} does not exist or is inactive."},
    )
    test_id = serializers.PrimaryKeyRelatedField(
        queryset=DiagnosticTest.objects.filter(is_active=True),
        error_messages={"does_not_exist": "Diagnostic test {pk_value} does not exist or is inactive."},
    )
    appointment_at = serializers.DateTimeField(
        help_text="ISO-8601 datetime. Naive values are interpreted in the server time zone (Asia/Kolkata)."
    )

    def validate_appointment_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("Appointment must be in the future.", code="appointment_in_past")
        if value > now + timedelta(days=settings.BOOKING_MAX_DAYS_AHEAD):
            raise serializers.ValidationError(
                f"Appointments can be booked at most {settings.BOOKING_MAX_DAYS_AHEAD} days ahead.",
                code="appointment_too_far",
            )
        return value

    def validate(self, attrs):
        offering = (
            CentreTest.objects.filter(centre=attrs["centre_id"], test=attrs["test_id"], is_available=True)
            .select_related("centre", "test")
            .first()
        )
        if offering is None:
            raise serializers.ValidationError(
                {"test_id": ["This test is not currently offered at the selected centre."]}, code="test_not_offered"
            )
        attrs["offering"] = offering
        return attrs
