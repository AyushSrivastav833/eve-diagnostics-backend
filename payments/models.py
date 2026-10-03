import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey("bookings.Booking", on_delete=models.PROTECT, related_name="payments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    provider = models.CharField(max_length=32, default="mockpay")
    # The provider's ID for this payment; webhooks refer to payments by this value.
    provider_reference = models.CharField(max_length=64, unique=True)
    # Client-supplied `Idempotency-Key` header, so a retried POST /payments/ never charges twice.
    # NULL (not "") when absent so the partial unique constraint below ignores payments without a key.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    failure_reason = models.CharField(max_length=255, blank=True)
    # Set when money was captured but the booking won't happen (cancelled before/after confirmation).
    needs_refund = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=Decimal("0")), name="payment_amount_positive"),
            # At most one in-flight or successful payment per booking -> no double charge, even under races.
            models.UniqueConstraint(
                fields=["booking"],
                condition=models.Q(status__in=[PaymentStatus.PENDING, PaymentStatus.SUCCESS]),
                name="one_active_payment_per_booking",
            ),
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_payment_idempotency_key_per_user",
            ),
        ]

    def __str__(self):
        return f"Payment {self.provider_reference} [{self.status}]"


class WebhookEventStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"  # stored, not yet (successfully) processed
    PROCESSED = "PROCESSED", "Processed"  # applied (or a harmless no-op)
    REJECTED = "REJECTED", "Rejected"  # permanently invalid (unknown payment, amount mismatch)
    FAILED = "FAILED", "Failed"  # transient error; will be retried


FINAL_WEBHOOK_STATUSES = {WebhookEventStatus.PROCESSED, WebhookEventStatus.REJECTED}


class WebhookEvent(models.Model):
    """
    Every webhook delivery we have accepted, keyed by the provider's event ID.

    The unique `event_id` is what makes the webhook idempotent: a redelivered
    event finds its existing row and gets back the stored response instead of
    being applied again.
    """

    event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=50)
    payment_reference = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    status = models.CharField(max_length=16, choices=WebhookEventStatus.choices, default=WebhookEventStatus.RECEIVED)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["status", "received_at"], name="webhook_status_received_idx")]

    def __str__(self):
        return f"{self.event_id} ({self.event_type}) [{self.status}]"

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_WEBHOOK_STATUSES
