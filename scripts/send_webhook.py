#!/usr/bin/env python
"""
Act as the payment provider: send a signed webhook to the API.

Standard library only, so it runs anywhere:

    python scripts/send_webhook.py --reference pay_mock_abc123 --amount 499.00 --status success
    python scripts/send_webhook.py --reference pay_mock_abc123 --amount 499.00 --event-id evt_1 --repeat 3
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", required=True, help="provider_reference returned by POST /payments/")
    parser.add_argument("--amount", required=True, help="Amount, must match the payment, e.g. 499.00")
    parser.add_argument("--status", choices=["success", "failed"], default="success")
    parser.add_argument("--event-id", default=None, help="Reuse an ID to test idempotency (default: random)")
    parser.add_argument("--repeat", type=int, default=1, help="Deliver the same event N times")
    parser.add_argument(
        "--url", default=os.environ.get("WEBHOOK_URL", "http://localhost:8000/api/v1/payments/webhook/")
    )
    parser.add_argument("--secret", default=os.environ.get("PAYMENT_WEBHOOK_SECRET", "dev-webhook-secret"))
    args = parser.parse_args()

    payload = {
        "event_id": args.event_id or f"evt_{uuid.uuid4().hex}",
        "type": "payment.succeeded" if args.status == "success" else "payment.failed",
        "data": {"payment_reference": args.reference, "amount": args.amount, "currency": "INR"},
    }
    if args.status == "failed":
        payload["data"]["failure_reason"] = "insufficient_funds"
    body = json.dumps(payload).encode()

    for attempt in range(1, args.repeat + 1):
        timestamp = str(int(time.time()))
        signature = hmac.new(args.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            args.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": signature,
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                status, text = response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode()
        print(f"[delivery {attempt}] HTTP {status} {text}")


if __name__ == "__main__":
    main()
