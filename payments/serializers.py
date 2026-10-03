from decimal import Decimal

from rest_framework import serializers

from .models import Payment, PaymentStatus
from .services import WEBHOOK_EVENT_TO_STATUS


class PaymentCreateSerializer(serializers.Serializer):
    booking_id = serializers.UUIDField()
    simulate_outcome = serializers.ChoiceField(
        choices=PaymentStatus.values,
        default=PaymentStatus.SUCCESS,
        help_text=(
            "Mock gateway behaviour. SUCCESS/FAILED resolve immediately; PENDING leaves the payment "
            "pending until a webhook reports the result."
        ),
    )


class PaymentSerializer(serializers.ModelSerializer):
    booking_id = serializers.UUIDField(read_only=True)
    booking_status = serializers.CharField(source="booking.status", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "booking_id",
            "booking_status",
            "amount",
            "currency",
            "status",
            "provider",
            "provider_reference",
            "failure_reason",
            "needs_refund",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields


class WebhookDataSerializer(serializers.Serializer):
    payment_reference = serializers.CharField(max_length=64)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    currency = serializers.CharField(max_length=3, default="INR")
    failure_reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class WebhookPayloadSerializer(serializers.Serializer):
    event_id = serializers.RegexField(r"^[A-Za-z0-9_.:-]{1,100}$", help_text="Provider's unique event ID")
    type = serializers.ChoiceField(choices=list(WEBHOOK_EVENT_TO_STATUS))
    data = WebhookDataSerializer()


class WebhookResponseSerializer(serializers.Serializer):
    event_id = serializers.CharField()
    status = serializers.ChoiceField(choices=["processed", "rejected"])
    duplicate = serializers.BooleanField()
    outcome = serializers.CharField(required=False)
    reason = serializers.CharField(required=False)
    payment_status = serializers.CharField(required=False)
    booking_status = serializers.CharField(required=False)
