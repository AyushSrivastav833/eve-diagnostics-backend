from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from catalog.models import DiagnosticTest
from payments.models import Payment, PaymentStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/bookings/"


def booking_payload(offering, when):
    return {"centre_id": offering.centre_id, "test_id": offering.test_id, "appointment_at": when.isoformat()}


def test_create_booking_uses_server_side_price(auth_client, user, offering, future_slot):
    payload = {**booking_payload(offering, future_slot), "amount": "1.00", "status": "CONFIRMED"}  # ignored
    response = auth_client.post(URL, payload, format="json")

    assert response.status_code == 201
    assert response.data["status"] == "PENDING"
    assert response.data["amount"] == "499.00"
    assert response.data["centre"]["id"] == offering.centre_id
    assert response.data["test"]["code"] == "CBC"
    booking = Booking.objects.get(pk=response.data["id"])
    assert booking.user == user


def test_price_change_does_not_affect_existing_booking(auth_client, offering, future_slot):
    booking_id = auth_client.post(URL, booking_payload(offering, future_slot), format="json").data["id"]
    offering.price = Decimal("999.00")
    offering.save()
    assert auth_client.get(f"{URL}{booking_id}/").data["amount"] == "499.00"


def test_booking_requires_authentication(api_client, offering, future_slot):
    response = api_client.post(URL, booking_payload(offering, future_slot), format="json")
    assert response.status_code == 401


def test_cannot_book_in_the_past(auth_client, offering):
    response = auth_client.post(URL, booking_payload(offering, timezone.now() - timedelta(hours=1)), format="json")
    assert response.status_code == 400
    assert "appointment_at" in response.data["error"]["details"]


def test_cannot_book_too_far_ahead(auth_client, offering, settings):
    when = timezone.now() + timedelta(days=settings.BOOKING_MAX_DAYS_AHEAD + 1)
    assert auth_client.post(URL, booking_payload(offering, when), format="json").status_code == 400


def test_cannot_book_test_not_offered_by_centre(auth_client, offering, future_slot):
    other_test = DiagnosticTest.objects.create(code="MRI", name="MRI Brain")
    payload = {**booking_payload(offering, future_slot), "test_id": other_test.id}
    response = auth_client.post(URL, payload, format="json")
    assert response.status_code == 400
    assert "test_id" in response.data["error"]["details"]


def test_cannot_book_unavailable_offering(auth_client, offering, future_slot):
    offering.is_available = False
    offering.save()
    assert auth_client.post(URL, booking_payload(offering, future_slot), format="json").status_code == 400


@pytest.mark.parametrize("field, value", [("centre_id", 999999), ("test_id", 999999), ("centre_id", "abc")])
def test_invalid_references_are_rejected(auth_client, offering, future_slot, field, value):
    payload = {**booking_payload(offering, future_slot), field: value}
    response = auth_client.post(URL, payload, format="json")
    assert response.status_code == 400
    assert field in response.data["error"]["details"]


def test_missing_fields_are_reported(auth_client):
    response = auth_client.post(URL, {}, format="json")
    assert response.status_code == 400
    assert set(response.data["error"]["details"]) == {"centre_id", "test_id", "appointment_at"}


def test_duplicate_active_booking_is_rejected(auth_client, offering, future_slot):
    assert auth_client.post(URL, booking_payload(offering, future_slot), format="json").status_code == 201
    response = auth_client.post(URL, booking_payload(offering, future_slot), format="json")
    assert response.status_code == 409
    assert response.data["error"]["code"] == "duplicate_booking"


def test_can_rebook_same_slot_after_cancelling(auth_client, offering, future_slot):
    first = auth_client.post(URL, booking_payload(offering, future_slot), format="json").data["id"]
    auth_client.post(f"{URL}{first}/cancel/")
    assert auth_client.post(URL, booking_payload(offering, future_slot), format="json").status_code == 201


def test_users_only_see_their_own_bookings(auth_client, other_client, booking):
    assert auth_client.get(URL).data["count"] == 1
    assert other_client.get(URL).data["count"] == 0
    # Someone else's booking looks exactly like a non-existent one.
    assert other_client.get(f"{URL}{booking.id}/").status_code == 404


def test_staff_can_see_all_bookings(staff_client, booking):
    assert staff_client.get(URL).data["count"] == 1


def test_filter_bookings_by_status(auth_client, booking):
    assert auth_client.get(URL, {"status": "pending"}).data["count"] == 1
    assert auth_client.get(URL, {"status": "CONFIRMED"}).data["count"] == 0


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_unknown_or_malformed_booking_id_returns_404(auth_client, bad_id):
    assert auth_client.get(f"{URL}{bad_id}/").status_code == 404
    assert auth_client.post(f"{URL}{bad_id}/cancel/").status_code == 404


def test_bookings_cannot_be_edited_or_deleted_directly(auth_client, booking):
    assert auth_client.patch(f"{URL}{booking.id}/", {"status": "CONFIRMED"}, format="json").status_code == 405
    assert auth_client.delete(f"{URL}{booking.id}/").status_code == 405


def test_cancel_pending_booking(auth_client, booking):
    response = auth_client.post(f"{URL}{booking.id}/cancel/")
    assert response.status_code == 200
    assert response.data["status"] == "CANCELLED"
    assert response.data["cancelled_at"] is not None


def test_cancel_twice_is_a_conflict(auth_client, booking):
    auth_client.post(f"{URL}{booking.id}/cancel/")
    response = auth_client.post(f"{URL}{booking.id}/cancel/")
    assert response.status_code == 409
    assert response.data["error"]["code"] == "invalid_booking_transition"


def test_cannot_cancel_failed_booking(auth_client, booking):
    booking.status = BookingStatus.FAILED
    booking.save()
    assert auth_client.post(f"{URL}{booking.id}/cancel/").status_code == 409


def test_cannot_cancel_someone_elses_booking(other_client, booking):
    assert other_client.post(f"{URL}{booking.id}/cancel/").status_code == 404
    booking.refresh_from_db()
    assert booking.status == BookingStatus.PENDING


def test_cancelling_confirmed_booking_flags_payment_for_refund(auth_client, booking):
    response = auth_client.post("/api/v1/payments/", {"booking_id": str(booking.id)}, format="json")
    assert response.data["booking_status"] == "CONFIRMED"

    assert auth_client.post(f"{URL}{booking.id}/cancel/").data["status"] == "CANCELLED"
    payment = Payment.objects.get(booking=booking)
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.needs_refund is True
