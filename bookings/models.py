import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from common.exceptions import ConflictError


class BookingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending payment"
    CONFIRMED = "CONFIRMED", "Confirmed"
    FAILED = "FAILED", "Payment failed"
    CANCELLED = "CANCELLED", "Cancelled"


# The only legal status changes. FAILED and CANCELLED are terminal: a patient whose
# payment failed simply creates a new booking (see README "Assumptions").
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    BookingStatus.PENDING: {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED},
    BookingStatus.CONFIRMED: {BookingStatus.CANCELLED},
    BookingStatus.FAILED: set(),
    BookingStatus.CANCELLED: set(),
}

ACTIVE_STATUSES = [BookingStatus.PENDING, BookingStatus.CONFIRMED]


class InvalidBookingTransition(ConflictError):
    default_code = "invalid_booking_transition"


class Booking(models.Model):
    # UUIDs (not sequential ints) so booking IDs can't be enumerated.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings")
    centre = models.ForeignKey("catalog.DiagnosticCentre", on_delete=models.PROTECT, related_name="bookings")
    test = models.ForeignKey("catalog.DiagnosticTest", on_delete=models.PROTECT, related_name="bookings")
    appointment_at = models.DateTimeField()
    # Price snapshot taken at booking time; later catalogue price changes don't affect it.
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=16, choices=BookingStatus.choices, default=BookingStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="booking_user_created_idx"),
            models.Index(fields=["status"], name="booking_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=Decimal("0")), name="booking_amount_positive"),
            models.CheckConstraint(condition=models.Q(status__in=BookingStatus.values), name="booking_status_valid"),
            # A user can't hold two live bookings for the same test/centre/slot (e.g. double-click).
            models.UniqueConstraint(
                fields=["user", "centre", "test", "appointment_at"],
                condition=models.Q(status__in=ACTIVE_STATUSES),
                name="unique_active_booking_per_slot",
            ),
        ]

    def __str__(self):
        return f"Booking {self.id} [{self.status}]"

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, new_status: str) -> None:
        """Change status in memory, enforcing the state machine. Caller saves."""
        if not self.can_transition_to(new_status):
            raise InvalidBookingTransition(
                f"Cannot move booking from {self.status} to {new_status}.",
                details={"current_status": self.status, "requested_status": new_status},
            )
        self.status = new_status
