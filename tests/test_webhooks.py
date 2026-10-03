import json
import time
from unittest import mock

import pytest

from bookings.models import BookingStatus
from payments.gateway import sign_webhook
from payments.models import Payment, PaymentStatus, WebhookEvent, WebhookEventStatus
from payments.services import reprocess_stuck_webhook_events
from tests.conftest import post_webhook, webhook_payload

pytestmark = pytest.mark.django_db


def assert_state(payment, payment_status, booking_status):
    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == payment_status
    assert payment.booking.status == booking_status


# --- Happy paths -------------------------------------------------------------


def test_success_webhook_confirms_booking(api_client, pending_payment):
    response = post_webhook(api_client, webhook_payload(pending_payment))

    assert response.status_code == 200
    assert response.data["status"] == "processed"
    assert response.data["outcome"] == "applied"
    assert response.data["duplicate"] is False
    assert response.data["booking_status"] == "CONFIRMED"
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_failed_webhook_marks_booking_failed(api_client, pending_payment):
    payload = webhook_payload(pending_payment, event_type="payment.failed", failure_reason="insufficient_funds")
    response = post_webhook(api_client, payload)

    assert response.status_code == 200
    assert_state(pending_payment, PaymentStatus.FAILED, BookingStatus.FAILED)
    assert Payment.objects.get(pk=pending_payment.pk).failure_reason == "insufficient_funds"


# --- Idempotency -------------------------------------------------------------


def test_same_event_delivered_many_times_is_applied_once(api_client, pending_payment):
    payload = webhook_payload(pending_payment, event_id="evt_repeat_1")

    responses = [post_webhook(api_client, payload) for _ in range(5)]

    assert [r.status_code for r in responses] == [200] * 5
    assert [r.data["duplicate"] for r in responses] == [False, True, True, True, True]
    assert WebhookEvent.objects.filter(event_id="evt_repeat_1").count() == 1
    assert WebhookEvent.objects.get(event_id="evt_repeat_1").attempts == 1
    assert Payment.objects.filter(booking=pending_payment.booking).count() == 1
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_different_events_with_same_result_are_harmless(api_client, pending_payment):
    post_webhook(api_client, webhook_payload(pending_payment))
    second = post_webhook(api_client, webhook_payload(pending_payment))  # new event_id, same outcome

    assert second.status_code == 200
    assert second.data["outcome"] == "already_applied"
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_conflicting_late_event_cannot_flip_final_state(api_client, pending_payment):
    post_webhook(api_client, webhook_payload(pending_payment, event_type="payment.succeeded"))
    late_failure = post_webhook(api_client, webhook_payload(pending_payment, event_type="payment.failed"))

    assert late_failure.status_code == 200
    assert late_failure.data["outcome"] == "ignored_payment_already_final"
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_webhook_after_synchronous_result_is_a_no_op(auth_client, api_client, booking):
    response = auth_client.post(
        "/api/v1/payments/", {"booking_id": str(booking.id), "simulate_outcome": "FAILED"}, format="json"
    )
    payment = Payment.objects.get(pk=response.data["id"])

    webhook = post_webhook(api_client, webhook_payload(payment, event_type="payment.succeeded"))
    assert webhook.data["outcome"] == "ignored_payment_already_final"
    assert_state(payment, PaymentStatus.FAILED, BookingStatus.FAILED)


def test_success_for_cancelled_booking_is_flagged_for_refund(auth_client, api_client, pending_payment):
    auth_client.post(f"/api/v1/bookings/{pending_payment.booking_id}/cancel/")
    response = post_webhook(api_client, webhook_payload(pending_payment))

    assert response.status_code == 200
    pending_payment.refresh_from_db()
    assert pending_payment.status == PaymentStatus.SUCCESS
    assert pending_payment.needs_refund is True
    assert response.data["booking_status"] == "CANCELLED"


# --- Authentication / signature ---------------------------------------------


def test_missing_signature_is_rejected(api_client, pending_payment):
    response = api_client.post("/api/v1/payments/webhook/", webhook_payload(pending_payment), format="json")
    assert response.status_code == 401
    assert response.data["error"]["code"] == "missing_signature"
    assert not WebhookEvent.objects.exists()


def test_wrong_secret_is_rejected(api_client, pending_payment):
    response = post_webhook(api_client, webhook_payload(pending_payment), secret="attacker-guess")
    assert response.status_code == 401
    assert response.data["error"]["code"] == "invalid_signature"
    assert_state(pending_payment, PaymentStatus.PENDING, BookingStatus.PENDING)


def test_tampered_body_is_rejected(api_client, pending_payment):
    original = webhook_payload(pending_payment, event_type="payment.failed")
    ts = str(int(time.time()))
    signature = sign_webhook(json.dumps(original).encode(), ts, "test-webhook-secret")
    tampered = {**original, "type": "payment.succeeded"}
    response = post_webhook(api_client, tampered, timestamp=ts, signature=signature)
    assert response.status_code == 401


def test_stale_timestamp_is_rejected(api_client, pending_payment):
    response = post_webhook(api_client, webhook_payload(pending_payment), timestamp=int(time.time()) - 3600)
    assert response.status_code == 401
    assert response.data["error"]["code"] == "stale_webhook"


# --- Invalid payloads / references ------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("event_id"),
        lambda p: p.update(type="payment.refunded"),
        lambda p: p["data"].pop("payment_reference"),
        lambda p: p["data"].update(amount="-5"),
        lambda p: p.update(event_id="bad id with spaces"),
    ],
)
def test_malformed_payload_is_rejected(api_client, pending_payment, mutate):
    payload = webhook_payload(pending_payment)
    mutate(payload)
    response = post_webhook(api_client, payload)
    assert response.status_code == 400
    assert not WebhookEvent.objects.exists()


def test_unknown_payment_reference(api_client, pending_payment):
    payload = webhook_payload(pending_payment)
    payload["data"]["payment_reference"] = "pay_mock_doesnotexist"

    first = post_webhook(api_client, payload)
    again = post_webhook(api_client, payload)

    assert first.status_code == 404
    assert first.data["reason"] == "unknown_payment"
    assert again.status_code == 404 and again.data["duplicate"] is True
    assert WebhookEvent.objects.get().status == WebhookEventStatus.REJECTED


def test_amount_mismatch_is_rejected_without_state_change(api_client, pending_payment):
    response = post_webhook(api_client, webhook_payload(pending_payment, amount="1.00"))

    assert response.status_code == 422
    assert response.data["reason"] == "amount_mismatch"
    assert_state(pending_payment, PaymentStatus.PENDING, BookingStatus.PENDING)


# --- Failure handling & retries ---------------------------------------------


def test_transient_failure_is_recorded_and_redelivery_succeeds(api_client, pending_payment):
    payload = webhook_payload(pending_payment, event_id="evt_flaky")

    with mock.patch("payments.services.apply_payment_result", side_effect=RuntimeError("db hiccup")):
        failed = post_webhook(api_client, payload)

    assert failed.status_code == 503
    assert failed.data["error"]["code"] == "webhook_processing_failed"
    event = WebhookEvent.objects.get(event_id="evt_flaky")
    assert event.status == WebhookEventStatus.FAILED
    assert event.attempts == 1
    assert "db hiccup" in event.last_error
    assert_state(pending_payment, PaymentStatus.PENDING, BookingStatus.PENDING)  # rolled back cleanly

    retried = post_webhook(api_client, payload)  # provider redelivers
    assert retried.status_code == 200
    assert retried.data["duplicate"] is False
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)


def test_failed_event_schedules_background_retry_with_backoff(api_client, pending_payment, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False  # pretend a broker is configured
    with (
        mock.patch("payments.services.apply_payment_result", side_effect=RuntimeError("boom")),
        mock.patch("payments.tasks.process_webhook_event_task.apply_async") as apply_async,
    ):
        post_webhook(api_client, webhook_payload(pending_payment))

    event = WebhookEvent.objects.get()
    apply_async.assert_called_once_with(args=[event.pk], countdown=30)


def test_retries_stop_after_max_attempts(api_client, pending_payment, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.WEBHOOK_MAX_ATTEMPTS = 1
    with (
        mock.patch("payments.services.apply_payment_result", side_effect=RuntimeError("boom")),
        mock.patch("payments.tasks.process_webhook_event_task.apply_async") as apply_async,
    ):
        post_webhook(api_client, webhook_payload(pending_payment))
    apply_async.assert_not_called()


def test_reprocess_job_recovers_failed_events(api_client, pending_payment):
    with mock.patch("payments.services.apply_payment_result", side_effect=RuntimeError("boom")):
        post_webhook(api_client, webhook_payload(pending_payment))

    summary = reprocess_stuck_webhook_events()

    assert summary == {"picked": 1, "succeeded": 1, "failed": 0}
    assert WebhookEvent.objects.get().status == WebhookEventStatus.PROCESSED
    assert_state(pending_payment, PaymentStatus.SUCCESS, BookingStatus.CONFIRMED)
