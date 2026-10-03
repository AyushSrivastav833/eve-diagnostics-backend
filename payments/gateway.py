"""
A fake payment provider ("MockPay").

It stands in for a real gateway such as Razorpay/Stripe. The caller decides
the outcome via `simulate_outcome`, which keeps demos and tests
deterministic:

* SUCCESS / FAILED - the provider answers synchronously.
* PENDING          - the provider accepts the payment but reports the final
                     result later through the webhook (the realistic async flow).

Real-world signing of webhooks is mirrored by `sign_webhook`, which the
webhook endpoint verifies and which `scripts/send_webhook.py` uses.
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from .models import PaymentStatus


@dataclass(frozen=True)
class ChargeResult:
    status: str
    failure_reason: str = ""


class MockPaymentGateway:
    name = "mockpay"

    def new_reference(self) -> str:
        return f"pay_mock_{uuid.uuid4().hex[:24]}"

    def charge(self, *, reference: str, amount: Decimal, currency: str, outcome: str) -> ChargeResult:
        if outcome == PaymentStatus.SUCCESS:
            return ChargeResult(PaymentStatus.SUCCESS)
        if outcome == PaymentStatus.FAILED:
            return ChargeResult(PaymentStatus.FAILED, failure_reason="card_declined")
        return ChargeResult(PaymentStatus.PENDING)


gateway = MockPaymentGateway()


def sign_webhook(body: bytes, timestamp: str, secret: str | None = None) -> str:
    """HMAC-SHA256 over `"{timestamp}.{raw body}"`, hex encoded (Stripe-style)."""
    key = (secret or settings.PAYMENT_WEBHOOK_SECRET).encode()
    return hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
