"""
Race-condition tests. They need real row locks and concurrent connections, so
they only run against PostgreSQL (docker compose / CI); SQLite skips them.
"""

import threading

import pytest
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from bookings.models import Booking, BookingStatus
from payments.models import Payment, PaymentStatus, WebhookEvent
from tests.conftest import client_for, post_webhook, webhook_payload

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL row locking"),
]


def run_concurrently(fn, n=8):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(fn())
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_concurrent_duplicate_webhooks_apply_once(pending_payment):
    payload = webhook_payload(pending_payment, event_id="evt_race")

    responses = run_concurrently(lambda: post_webhook(APIClient(), payload))

    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.data["duplicate"] is False) == 1
    assert WebhookEvent.objects.filter(event_id="evt_race").count() == 1
    pending_payment.refresh_from_db()
    assert pending_payment.status == PaymentStatus.SUCCESS
    assert Booking.objects.get(pk=pending_payment.booking_id).status == BookingStatus.CONFIRMED


def test_concurrent_payment_attempts_charge_once(user, booking):
    responses = run_concurrently(
        lambda: client_for(user).post("/api/v1/payments/", {"booking_id": str(booking.id)}, format="json")
    )

    assert sorted(r.status_code for r in responses).count(201) == 1
    assert all(r.status_code in (201, 409) for r in responses)
    assert Payment.objects.filter(booking=booking).count() == 1


def test_concurrent_identical_bookings_create_one(user, offering, future_slot):
    payload = {"centre_id": offering.centre_id, "test_id": offering.test_id, "appointment_at": future_slot.isoformat()}

    responses = run_concurrently(lambda: client_for(user).post("/api/v1/bookings/", payload, format="json"))

    assert [r.status_code for r in responses].count(201) == 1
    assert Booking.objects.filter(user=user).count() == 1
