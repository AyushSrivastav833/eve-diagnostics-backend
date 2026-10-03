#!/usr/bin/env python
"""
End-to-end walkthrough against a running server (standard library only):

  signup -> browse centres -> book -> pay (provider says PENDING)
  -> provider webhook delivered 3x -> booking CONFIRMED exactly once

    python manage.py seed_catalog          # once
    python manage.py runserver             # in another terminal
    python scripts/demo_flow.py
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = os.environ.get("API_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None, headers=None):
    request = urllib.request.Request(f"{BASE}{path}", method=method, data=json.dumps(body).encode() if body else None)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def step(title, status, data):
    print(f"\n=== {title} -> HTTP {status}")
    print(json.dumps(data, indent=2)[:1200])


def main():
    email = f"demo-{uuid.uuid4().hex[:8]}@example.com"
    status, data = call(
        "POST", "/auth/signup/", {"email": email, "password": "Demo-Passw0rd!", "full_name": "Demo User"}
    )
    step("Sign up", status, data)
    token = data["access"]

    status, data = call("GET", "/centres/?test=CBC")
    step("Centres offering CBC", status, data)
    if not data.get("results"):
        sys.exit("No centres found - run `python manage.py seed_catalog` first.")
    centre = data["results"][0]
    test = next(o["test"] for o in centre["tests"] if o["test"]["code"] == "CBC")

    when = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat()
    status, booking = call(
        "POST", "/bookings/", {"centre_id": centre["id"], "test_id": test["id"], "appointment_at": when}, token
    )
    step("Create booking", status, booking)

    status, payment = call(
        "POST",
        "/payments/",
        {"booking_id": booking["id"], "simulate_outcome": "PENDING"},
        token,
        headers={"Idempotency-Key": f"demo-{booking['id']}"},
    )
    step("Pay (provider responds PENDING)", status, payment)

    print("\n=== Provider delivers the SAME webhook event 3 times")
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("send_webhook.py")),
            "--reference",
            payment["provider_reference"],
            "--amount",
            payment["amount"],
            "--event-id",
            f"evt_demo_{uuid.uuid4().hex[:8]}",
            "--repeat",
            "3",
            "--url",
            f"{BASE}/payments/webhook/",
        ],
        check=True,
    )

    status, data = call("GET", f"/bookings/{booking['id']}/", token=token)
    step("Booking after webhooks", status, data)
    status, data = call("GET", "/payments/", token=token)
    print(f"\nPayments for this user: {data['count']} (expected 1)")


if __name__ == "__main__":
    main()
