"""
Payment use-cases.

Concurrency rules used throughout:

* Row locks are always taken in the order  webhook event -> booking -> payment,
  so concurrent flows can't deadlock.
* Every state change is guarded by the current state (only PENDING payments /
  bookings move), so applying the same result twice is a no-op.
* Partial unique indexes back up the application checks under races.
"""

import hmac
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound

from bookings.models import Booking, BookingStatus
from common.exceptions import ConflictError, UnprocessableError

from .gateway import gateway, sign_webhook
from .models import Payment, PaymentStatus, WebhookEvent, WebhookEventStatus

logger = logging.getLogger(__name__)

WEBHOOK_EVENT_TO_STATUS = {
    "payment.succeeded": PaymentStatus.SUCCESS,
    "payment.failed": PaymentStatus.FAILED,
}


# ---------------------------------------------------------------------------
# Initiating a payment
# ---------------------------------------------------------------------------
def initiate_payment(*, user, booking_id, outcome: str, idempotency_key: str | None = None) -> tuple[Payment, bool]:
    """
    Create a payment for one of `user`'s bookings and charge it through the mock gateway.

    Returns `(payment, created)`. `created` is False when the request is a replay
    of an earlier one with the same `Idempotency-Key`; the original payment is
    returned and nothing is charged again.
    """
    if idempotency_key and (existing := _find_by_idempotency_key(user, idempotency_key, booking_id)):
        return existing, False

    try:
        with transaction.atomic():
            try:
                # Lock the booking so it can't be cancelled or paid twice concurrently.
                booking = Booking.objects.select_for_update().get(pk=booking_id, user=user)
            except Booking.DoesNotExist:
                raise NotFound("Booking not found.", code="booking_not_found") from None
            _ensure_payable(booking)
            payment = Payment.objects.create(
                booking=booking,
                user=user,
                amount=booking.amount,  # always charge the server-side booking amount
                currency=booking.currency,
                status=PaymentStatus.PENDING,
                provider=gateway.name,
                provider_reference=gateway.new_reference(),
                idempotency_key=idempotency_key,
            )
    except IntegrityError as exc:
        # Lost a race: a concurrent request created a payment for this booking / key first.
        if idempotency_key and (existing := _find_by_idempotency_key(user, idempotency_key, booking_id)):
            return existing, False
        raise ConflictError("A payment for this booking is already in progress.", code="payment_in_progress") from exc

    logger.info(
        "payment_initiated",
        extra={"payment_id": str(payment.id), "booking_id": str(booking_id), "amount": str(payment.amount)},
    )

    # Talk to the "provider" outside the DB transaction: a real network call must not hold row locks.
    result = gateway.charge(
        reference=payment.provider_reference, amount=payment.amount, currency=payment.currency, outcome=outcome
    )
    if result.status != PaymentStatus.PENDING:
        apply_payment_result(payment_id=payment.pk, new_status=result.status, failure_reason=result.failure_reason)
        payment.refresh_from_db()
    return payment, True


def _find_by_idempotency_key(user, key: str, booking_id) -> Payment | None:
    existing = Payment.objects.filter(user=user, idempotency_key=key).first()
    if existing is not None and str(existing.booking_id) != str(booking_id):
        raise UnprocessableError(
            "This Idempotency-Key was already used for a different booking.", code="idempotency_key_reused"
        )
    return existing


def _ensure_payable(booking: Booking) -> None:
    if booking.status != BookingStatus.PENDING:
        raise ConflictError(
            f"Booking is {booking.status} and cannot be paid.",
            code="booking_not_payable",
            details={"booking_status": booking.status},
        )
    if booking.appointment_at <= timezone.now():
        raise ConflictError("The appointment time has passed; please create a new booking.", code="booking_expired")
    in_flight = booking.payments.filter(status=PaymentStatus.PENDING).first()
    if in_flight is not None:
        raise ConflictError(
            "A payment for this booking is already in progress.",
            code="payment_in_progress",
            details={"payment_id": str(in_flight.id)},
        )


# ---------------------------------------------------------------------------
# Applying a final result (from the gateway response or from a webhook)
# ---------------------------------------------------------------------------
def apply_payment_result(*, payment_id, new_status: str, failure_reason: str = "") -> str:
    """
    Move a PENDING payment to SUCCESS/FAILED and update its booking.

    Safe to call any number of times with the same result. Returns a short
    outcome code describing what happened.
    """
    booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=payment_id)
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)

        if payment.status == new_status:
            return "already_applied"
        if payment.status != PaymentStatus.PENDING:
            # e.g. a "failed" event arriving after we already recorded success. Never flip a final state.
            logger.warning(
                "payment_result_conflict",
                extra={"payment_id": str(payment.id), "current": payment.status, "incoming": new_status},
            )
            return "ignored_payment_already_final"

        payment.status = new_status
        payment.completed_at = timezone.now()
        if new_status == PaymentStatus.FAILED:
            payment.failure_reason = (failure_reason or "payment_failed")[:255]

        if booking.status == BookingStatus.PENDING:
            booking.transition_to(
                BookingStatus.CONFIRMED if new_status == PaymentStatus.SUCCESS else BookingStatus.FAILED
            )
            booking.save(update_fields=["status", "updated_at"])
        elif new_status == PaymentStatus.SUCCESS:
            # Money captured for a booking that was cancelled meanwhile -> flag for refund.
            payment.needs_refund = True
            logger.warning(
                "payment_succeeded_for_inactive_booking",
                extra={"payment_id": str(payment.id), "booking_status": booking.status},
            )
        payment.save()

    logger.info(
        "payment_result_applied",
        extra={"payment_id": str(payment.id), "payment_status": payment.status, "booking_status": booking.status},
    )
    return "applied"


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
class WebhookSignatureError(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "invalid_signature"
    default_detail = "Webhook signature verification failed."


@dataclass(frozen=True)
class WebhookResult:
    status_code: int
    body: dict


def verify_webhook_signature(*, body: bytes, timestamp: str | None, signature: str | None) -> None:
    if not timestamp or not signature:
        raise WebhookSignatureError(
            "Missing X-Webhook-Timestamp or X-Webhook-Signature header.", code="missing_signature"
        )
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise WebhookSignatureError("Malformed webhook timestamp.") from None
    if abs(time.time() - sent_at) > settings.PAYMENT_WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookSignatureError("Webhook timestamp is outside the allowed window.", code="stale_webhook")
    if not hmac.compare_digest(sign_webhook(body, timestamp), signature):
        raise WebhookSignatureError()


def receive_webhook(payload: dict) -> WebhookResult:
    """Store the event (once per event_id) and process it."""
    event, created = WebhookEvent.objects.get_or_create(
        event_id=payload["event_id"],
        defaults={
            "event_type": payload["type"],
            "payment_reference": payload["data"]["payment_reference"],
            "payload": payload,
        },
    )
    logger.info(
        "webhook_received",
        extra={"event_id": event.event_id, "event_type": event.event_type, "redelivery": not created},
    )
    return process_webhook_event(event.pk)


def process_webhook_event(event_pk: int) -> WebhookResult:
    """
    Apply a stored webhook event exactly once.

    The event row is locked for the duration, so concurrent deliveries of the
    same event are serialised: the first applies it, the rest see a final
    status and get the stored response back.
    """
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.select_for_update().get(pk=event_pk)
            if event.is_final:
                logger.info("webhook_duplicate_ignored", extra={"event_id": event.event_id})
                return WebhookResult(event.response_status, {**event.response_body, "duplicate": True})

            event.attempts += 1
            event_status, status_code, body = _apply_event(event)
            event.status = event_status
            event.response_status = status_code
            event.response_body = body
            event.processed_at = timezone.now()
            event.last_error = ""
            event.save()
    except Exception as exc:
        _record_webhook_failure(event_pk, exc)
        raise

    logger.info("webhook_processed", extra={"event_id": event.event_id, "result": event.status})
    return WebhookResult(status_code, {**body, "duplicate": False})


def _apply_event(event: WebhookEvent) -> tuple[str, int, dict]:
    base = {"event_id": event.event_id}
    payment = Payment.objects.filter(provider_reference=event.payment_reference).first()
    if payment is None:
        logger.warning("webhook_unknown_payment", extra={"event_id": event.event_id})
        return WebhookEventStatus.REJECTED, 404, {**base, "status": "rejected", "reason": "unknown_payment"}
    event.payment = payment

    data = event.payload.get("data", {})
    try:
        amount = Decimal(str(data.get("amount")))
    except InvalidOperation:
        amount = None
    currency = str(data.get("currency") or payment.currency).upper()
    if amount != payment.amount or currency != payment.currency:
        logger.warning(
            "webhook_amount_mismatch",
            extra={"event_id": event.event_id, "expected": str(payment.amount), "received": str(amount)},
        )
        return WebhookEventStatus.REJECTED, 422, {**base, "status": "rejected", "reason": "amount_mismatch"}

    outcome = apply_payment_result(
        payment_id=payment.pk,
        new_status=WEBHOOK_EVENT_TO_STATUS[event.event_type],
        failure_reason=data.get("failure_reason", ""),
    )
    payment.refresh_from_db()
    booking_status = Booking.objects.values_list("status", flat=True).get(pk=payment.booking_id)
    return (
        WebhookEventStatus.PROCESSED,
        200,
        {
            **base,
            "status": "processed",
            "outcome": outcome,
            "payment_id": str(payment.pk),
            "payment_status": payment.status,
            "booking_id": str(payment.booking_id),
            "booking_status": booking_status,
        },
    )


def _record_webhook_failure(event_pk: int, exc: Exception) -> None:
    WebhookEvent.objects.filter(pk=event_pk).update(
        status=WebhookEventStatus.FAILED,
        attempts=F("attempts") + 1,
        last_error=f"{type(exc).__name__}: {exc}"[:2000],
    )
    logger.error("webhook_processing_failed", extra={"event_pk": event_pk}, exc_info=exc)


def schedule_webhook_retry(event_pk: int) -> bool:
    """Queue a background retry with exponential backoff. Returns True if one was queued."""
    event = WebhookEvent.objects.get(pk=event_pk)
    if event.is_final:
        return False
    if event.attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
        logger.error("webhook_retry_exhausted", extra={"event_id": event.event_id, "attempts": event.attempts})
        return False
    if settings.CELERY_TASK_ALWAYS_EAGER:
        # No broker configured (local dev): rely on provider redelivery / `manage.py reprocess_webhooks`.
        logger.info("webhook_retry_deferred", extra={"event_id": event.event_id})
        return False

    from .tasks import process_webhook_event_task

    countdown = min(30 * 2 ** max(event.attempts - 1, 0), 3600)
    process_webhook_event_task.apply_async(args=[event_pk], countdown=countdown)
    logger.info("webhook_retry_scheduled", extra={"event_id": event.event_id, "countdown": countdown})
    return True


def reprocess_stuck_webhook_events(*, limit: int = 100, min_age_seconds: int = 60) -> dict:
    """Safety net (run periodically): retry FAILED events and RECEIVED ones that never finished."""
    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)
    pks = list(
        WebhookEvent.objects.filter(attempts__lt=settings.WEBHOOK_MAX_ATTEMPTS)
        .filter(Q(status=WebhookEventStatus.FAILED) | Q(status=WebhookEventStatus.RECEIVED, received_at__lte=cutoff))
        .order_by("received_at")
        .values_list("pk", flat=True)[:limit]
    )
    summary = {"picked": len(pks), "succeeded": 0, "failed": 0}
    for pk in pks:
        try:
            process_webhook_event(pk)
            summary["succeeded"] += 1
        except Exception:
            summary["failed"] += 1
    logger.info("webhook_reprocess_run", extra=summary)
    return summary
