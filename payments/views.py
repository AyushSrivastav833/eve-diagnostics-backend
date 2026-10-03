import logging
import re

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Payment, WebhookEvent
from .serializers import (
    PaymentCreateSerializer,
    PaymentSerializer,
    WebhookPayloadSerializer,
    WebhookResponseSerializer,
)
from .services import initiate_payment, receive_webhook, schedule_webhook_retry, verify_webhook_signature

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@extend_schema_view(
    list=extend_schema(summary="List my payments"),
    retrieve=extend_schema(summary="Get one of my payments"),
)
class PaymentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]
    throttle_scope = "payments"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation, no real user
            return Payment.objects.none()
        qs = Payment.objects.select_related("booking")
        if not self.request.user.is_staff:
            qs = qs.filter(user=self.request.user)
        return qs

    @extend_schema(
        summary="Pay for a booking (simulated)",
        request=PaymentCreateSerializer,
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                description="Optional. Same key on retry returns the original payment; no second charge.",
            )
        ],
        responses={
            201: PaymentSerializer,
            200: OpenApiResponse(PaymentSerializer, description="Idempotent replay of an earlier request"),
            404: OpenApiResponse(description="Booking not found (or not yours)"),
            409: OpenApiResponse(description="Booking not payable / payment already in progress / expired"),
            422: OpenApiResponse(description="Idempotency-Key reused for a different booking"),
        },
    )
    def create(self, request):
        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise ValidationError({"Idempotency-Key": ["Must be 1-64 characters of [A-Za-z0-9_.:-]."]})

        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment, created = initiate_payment(
            user=request.user,
            booking_id=serializer.validated_data["booking_id"],
            outcome=serializer.validated_data["simulate_outcome"],
            idempotency_key=idempotency_key,
        )
        payment = Payment.objects.select_related("booking").get(pk=payment.pk)
        response = Response(
            PaymentSerializer(payment).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
        if not created:
            response["Idempotent-Replayed"] = "true"
        return response


class PaymentWebhookView(APIView):
    """
    Receives payment status updates from the (simulated) provider.

    Authenticated by an HMAC signature rather than a user token. Idempotent on
    `event_id`: redeliveries return the original response with `duplicate: true`
    and never change state again.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "webhooks"

    @extend_schema(
        summary="Payment provider webhook",
        request=WebhookPayloadSerializer,
        parameters=[
            OpenApiParameter("X-Webhook-Timestamp", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True),
            OpenApiParameter(
                "X-Webhook-Signature",
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=True,
                description='hex(HMAC_SHA256(secret, "{timestamp}.{raw_body}"))',
            ),
        ],
        responses={
            200: WebhookResponseSerializer,
            400: OpenApiResponse(description="Malformed payload"),
            401: OpenApiResponse(description="Missing / invalid / stale signature"),
            404: OpenApiResponse(WebhookResponseSerializer, description="Unknown payment reference"),
            422: OpenApiResponse(WebhookResponseSerializer, description="Amount/currency mismatch"),
            503: OpenApiResponse(description="Temporary failure; the event is stored and will be retried"),
        },
    )
    def post(self, request):
        # Verify against the exact raw bytes that were signed, before trusting any of the content.
        verify_webhook_signature(
            body=request.body,
            timestamp=request.headers.get("X-Webhook-Timestamp"),
            signature=request.headers.get("X-Webhook-Signature"),
        )
        serializer = WebhookPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = receive_webhook(request.data)
        except Exception:
            # The failure is recorded on the stored event; retry it in the background and ask the
            # provider to redeliver too (non-2xx). Either path is safe thanks to idempotency.
            event = WebhookEvent.objects.filter(event_id=request.data["event_id"]).first()
            if event is not None:
                schedule_webhook_retry(event.pk)
            return Response(
                {
                    "error": {
                        "code": "webhook_processing_failed",
                        "message": "Temporary failure while processing the event; it will be retried.",
                        "details": {},
                    }
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result.body, status=result.status_code)
