import pytest

from bookings.models import Booking, BookingStatus, InvalidBookingTransition

ALLOWED = {
    ("PENDING", "CONFIRMED"),
    ("PENDING", "FAILED"),
    ("PENDING", "CANCELLED"),
    ("CONFIRMED", "CANCELLED"),
}


@pytest.mark.parametrize("current", BookingStatus.values)
@pytest.mark.parametrize("target", BookingStatus.values)
def test_booking_state_machine(current, target):
    booking = Booking(status=current)
    if (current, target) in ALLOWED:
        booking.transition_to(target)
        assert booking.status == target
    else:
        with pytest.raises(InvalidBookingTransition):
            booking.transition_to(target)
        assert booking.status == current
