import json
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from bookings.models import Booking
from catalog.models import CentreTest, DiagnosticCentre, DiagnosticTest
from payments.gateway import sign_webhook

PASSWORD = "S3cure-Passw0rd!"


@pytest.fixture(autouse=True)
def _test_settings(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.PAYMENT_WEBHOOK_SECRET = "test-webhook-secret"
    cache.clear()  # resets catalogue cache and throttle counters between tests
    yield
    cache.clear()


@pytest.fixture
def api_client():
    return APIClient()


def make_user(email="patient@example.com", **extra):
    return User.objects.create_user(
        email=email, password=PASSWORD, full_name=extra.pop("full_name", "Test Patient"), **extra
    )


@pytest.fixture
def user(db):
    return make_user()


@pytest.fixture
def other_user(db):
    return make_user(email="other@example.com", full_name="Other Patient")


@pytest.fixture
def staff_user(db):
    return make_user(email="staff@example.com", full_name="Staff Member", is_staff=True)


def client_for(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


@pytest.fixture
def auth_client(user):
    return client_for(user)


@pytest.fixture
def other_client(other_user):
    return client_for(other_user)


@pytest.fixture
def staff_client(staff_user):
    return client_for(staff_user)


@pytest.fixture
def centre(db):
    return DiagnosticCentre.objects.create(name="EVE Gangtok", address="12 MG Marg", city="Gangtok", pincode="737101")


@pytest.fixture
def test_cbc(db):
    return DiagnosticTest.objects.create(code="CBC", name="Complete Blood Count")


@pytest.fixture
def offering(centre, test_cbc):
    return CentreTest.objects.create(centre=centre, test=test_cbc, price=Decimal("499.00"))


@pytest.fixture
def future_slot():
    return (timezone.now() + timedelta(days=2)).replace(microsecond=0)


@pytest.fixture
def booking(user, offering, future_slot):
    return Booking.objects.create(
        user=user, centre=offering.centre, test=offering.test, appointment_at=future_slot, amount=offering.price
    )


@pytest.fixture
def pending_payment(auth_client, booking):
    """A payment the mock gateway left PENDING; its result must arrive by webhook."""
    response = auth_client.post(
        "/api/v1/payments/", {"booking_id": str(booking.id), "simulate_outcome": "PENDING"}, format="json"
    )
    assert response.status_code == 201, response.content
    from payments.models import Payment

    return Payment.objects.get(pk=response.data["id"])


def webhook_payload(payment, *, event_type="payment.succeeded", event_id=None, amount=None, **data):
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "data": {
            "payment_reference": payment.provider_reference,
            "amount": str(amount if amount is not None else payment.amount),
            "currency": "INR",
            **data,
        },
    }


def post_webhook(client, payload, *, secret="test-webhook-secret", timestamp=None, signature=None):
    body = json.dumps(payload).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = signature if signature is not None else sign_webhook(body, ts, secret)
    return client.generic(
        "POST",
        "/api/v1/payments/webhook/",
        body,
        content_type="application/json",
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
        HTTP_X_WEBHOOK_SIGNATURE=sig,
    )
