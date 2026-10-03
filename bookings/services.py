"""Booking use-cases. Views stay thin; all state changes go through here."""

import logging
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.models import CentreTest
from common.exceptions import ConflictError
from payments.models import PaymentStatus

from .models import ACTIVE_STATUSES, Booking, BookingStatus

logger = logging.getLogger(__name__)


def create_booking(*, user, offering: CentreTest, appointment_at: datetime) -> Booking:
    duplicate_error = ConflictError(
        "You already have an active booking for this test at this centre and time.", code="duplicate_booking"
    )
    if Booking.objects.filter(
        user=user,
        centre=offering.centre,
        test=offering.test,
        appointment_at=appointment_at,
        status__in=ACTIVE_STATUSES,
    ).exists():
        raise duplicate_error

    try:
        with transaction.atomic():
            booking = Booking.objects.create(
                user=user,
                centre=offering.centre,
                test=offering.test,
                appointment_at=appointment_at,
                amount=offering.price,  # server-side price; never trust a client-supplied amount
                status=BookingStatus.PENDING,
            )
    except IntegrityError as exc:
        # Lost a race with a concurrent identical request; the partial unique index caught it.
        raise duplicate_error from exc

    logger.info(
        "booking_created",
        extra={"booking_id": str(booking.id), "user_id": user.id, "amount": str(booking.amount)},
    )
    return booking


def cancel_booking(*, booking_id) -> Booking:
    with transaction.atomic():
        # Row lock so a cancel can't interleave with a payment result for the same booking.
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        previous_status = booking.status
        booking.transition_to(BookingStatus.CANCELLED)
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=["status", "cancelled_at", "updated_at"])

        if previous_status == BookingStatus.CONFIRMED:
            # Money was taken for a booking that will no longer happen.
            booking.payments.filter(status=PaymentStatus.SUCCESS).update(needs_refund=True, updated_at=timezone.now())

    logger.info("booking_cancelled", extra={"booking_id": str(booking.id), "previous_status": previous_status})
    return booking
