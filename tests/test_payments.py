from datetime import timedelta

import pytest
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from payments.models import Payment, PaymentStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/payments/"


def pay(client, booking, outcome=None, key=None):
    body = {"booking_id": str(booking.id)}
    if outcome:
        body["simulate_outcome"] = outcome
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return client.post(URL, body, format="json", **headers)


def test_successful_payment_confirms_booking(auth_client, booking):
    response = pay(auth_client, booking, "SUCCESS")

    assert response.status_code == 201
    assert response.data["status"] == "SUCCESS"
    assert response.data["amount"] == "499.00"
    assert response.data["booking_status"] == "CONFIRMED"
    assert response.data["provider_reference"].startswith("pay_mock_")
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CONFIRMED


def test_default_outcome_is_success(auth_client, booking):
    assert pay(auth_client, booking).data["status"] == "SUCCESS"


def test_failed_payment_marks_booking_failed(auth_client, booking):
    response = pay(auth_client, booking, "FAILED")

    assert response.status_code == 201
    assert response.data["status"] == "FAILED"
    assert response.data["failure_reason"] == "card_declined"
    assert response.data["booking_status"] == "FAILED"


def test_pending_payment_leaves_booking_pending(auth_client, booking):
    response = pay(auth_client, booking, "PENDING")
    assert response.data["status"] == "PENDING"
    assert response.data["booking_status"] == "PENDING"


def test_payment_requires_authentication(api_client, booking):
    assert pay(api_client, booking).status_code == 401


def test_cannot_pay_someone_elses_booking(other_client, booking):
    response = pay(other_client, booking)
    assert response.status_code == 404
    assert response.data["error"]["code"] == "booking_not_found"
    assert not Payment.objects.exists()


def test_unknown_booking_id_returns_404(auth_client):
    response = auth_client.post(URL, {"booking_id": "00000000-0000-0000-0000-000000000000"}, format="json")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [{}, {"booking_id": "abc"}, {"booking_id": "00000000-0000-0000-0000-000000000000", "simulate_outcome": "MAYBE"}],
)
def test_invalid_payment_requests_are_rejected(auth_client, body):
    response = auth_client.post(URL, body, format="json")
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


def test_cannot_pay_confirmed_booking_twice(auth_client, booking):
    pay(auth_client, booking)
    response = pay(auth_client, booking)
    assert response.status_code == 409
    assert response.data["error"]["code"] == "booking_not_payable"
    assert Payment.objects.filter(booking=booking).count() == 1


@pytest.mark.parametrize("booking_status", [BookingStatus.CANCELLED, BookingStatus.FAILED])
def test_cannot_pay_terminal_booking(auth_client, booking, booking_status):
    booking.status = booking_status
    booking.save()
    assert pay(auth_client, booking).status_code == 409


def test_cannot_start_second_payment_while_one_is_pending(auth_client, booking):
    first = pay(auth_client, booking, "PENDING")
    response = pay(auth_client, booking, "SUCCESS")
    assert response.status_code == 409
    assert response.data["error"]["code"] == "payment_in_progress"
    assert response.data["error"]["details"]["payment_id"] == str(first.data["id"])


def test_cannot_pay_for_expired_appointment(auth_client, booking):
    Booking.objects.filter(pk=booking.pk).update(appointment_at=timezone.now() - timedelta(minutes=1))
    response = pay(auth_client, booking)
    assert response.status_code == 409
    assert response.data["error"]["code"] == "booking_expired"


def test_idempotency_key_replay_returns_original_payment(auth_client, booking):
    first = pay(auth_client, booking, "SUCCESS", key="order-123")
    replay = pay(auth_client, booking, "SUCCESS", key="order-123")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay["Idempotent-Replayed"] == "true"
    assert replay.data["id"] == first.data["id"]
    assert Payment.objects.count() == 1


def test_idempotency_key_reused_for_other_booking_is_rejected(auth_client, user, booking, offering, future_slot):
    other_booking = Booking.objects.create(
        user=user,
        centre=offering.centre,
        test=offering.test,
        appointment_at=future_slot + timedelta(hours=1),
        amount=offering.price,
    )
    pay(auth_client, booking, key="k1")
    response = pay(auth_client, other_booking, key="k1")
    assert response.status_code == 422
    assert response.data["error"]["code"] == "idempotency_key_reused"


def test_idempotency_keys_are_scoped_per_user(auth_client, other_client, other_user, booking, offering, future_slot):
    other_booking = Booking.objects.create(
        user=other_user, centre=offering.centre, test=offering.test, appointment_at=future_slot, amount=offering.price
    )
    assert pay(auth_client, booking, key="same").status_code == 201
    assert pay(other_client, other_booking, key="same").status_code == 201


def test_malformed_idempotency_key_is_rejected(auth_client, booking):
    assert pay(auth_client, booking, key="x" * 65).status_code == 400


def test_users_only_list_their_own_payments(auth_client, other_client, booking):
    payment_id = pay(auth_client, booking).data["id"]
    assert auth_client.get(URL).data["count"] == 1
    assert other_client.get(URL).data["count"] == 0
    assert other_client.get(f"{URL}{payment_id}/").status_code == 404
    assert auth_client.get(f"{URL}{payment_id}/").data["status"] == PaymentStatus.SUCCESS
