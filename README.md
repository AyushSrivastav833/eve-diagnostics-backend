# EVE Diagnostics – Booking & Payments Backend

A backend service that lets patients book diagnostic tests at partner centres and pay through a **simulated payment provider**, with an **idempotent, signed payment webhook**.

Built with **Django 5.2 + Django REST Framework**, **PostgreSQL**, **Redis** (cache and rate limiting), **Celery** (webhook retries), JWT auth and OpenAPI docs.

| | |
|---|---|
| Tests | 120 tests, ~95% coverage, including real-concurrency tests on PostgreSQL |
| Docs | Swagger UI at `/api/docs/`, ReDoc at `/api/redoc/` |
| Run | `docker compose up --build` |

---

## Contents
1. [Quick start](#1-quick-start)
2. [Architecture](#2-architecture)
3. [Database design](#3-database-design)
4. [API reference & example requests](#4-api-reference--example-requests)
5. [Payments & the idempotent webhook](#5-payments--the-idempotent-webhook)
6. [Edge cases handled](#6-edge-cases-handled)
7. [Testing](#7-testing)
8. [Assumptions](#8-assumptions)
9. [What I would improve with more time](#9-what-i-would-improve-with-more-time)

---

## 1. Quick start

### Option A: Docker (recommended: PostgreSQL + Redis + Celery)

```bash
cp .env.example .env                      # optional; sensible defaults are built in
docker compose up --build -d              # db, redis, web (migrates on start), worker (+beat)
docker compose exec web python manage.py seed_catalog --with-users
```

* API: <http://localhost:8000/api/v1/>
* Swagger UI: <http://localhost:8000/api/docs/>
* Django admin: <http://localhost:8000/admin/> (`admin@eve.local` / `Admin@12345`)
* Run tests inside the container: `docker compose exec web pytest`

### Option B: Local without Docker (SQLite, no Redis needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py seed_catalog --with-users     # demo centres/tests + admin & patient users
DEBUG=true python manage.py runserver
```

With no `DATABASE_URL` / `REDIS_URL` / `CELERY_BROKER_URL` set, the app falls back to SQLite, an in-memory cache and inline (eager) task execution, so it runs with zero infrastructure. To use PostgreSQL locally, set
`DATABASE_URL=postgres://user:pass@localhost:5432/eve`.

### See the whole flow in one command

With the server running:

```bash
python scripts/demo_flow.py
```

It signs up a user, finds a centre, books a CBC test, pays with the provider answering *PENDING*, then **delivers the same webhook event three times** and shows the booking was confirmed exactly once:

```
[delivery 1] HTTP 200 {"status":"processed","outcome":"applied","booking_status":"CONFIRMED","duplicate":false,...}
[delivery 2] HTTP 200 {"status":"processed","outcome":"applied","booking_status":"CONFIRMED","duplicate":true,...}
[delivery 3] HTTP 200 {"status":"processed","outcome":"applied","booking_status":"CONFIRMED","duplicate":true,...}
Payments for this user: 1 (expected 1)
```

### Demo accounts (`seed_catalog --with-users`)
| Role | Email | Password |
|---|---|---|
| Staff (manages catalogue) | `admin@eve.local` | `Admin@12345` |
| Patient | `patient@eve.local` | `Patient@12345` |

---

## 2. Architecture

```
config/      settings (12-factor, env driven), urls, celery app
common/      error envelope, JSON logging, request-ID middleware, pagination, permissions, health check
accounts/    custom User (email login), signup / login / refresh / me
catalog/     DiagnosticCentre, DiagnosticTest, CentreTest (price per centre) + Redis read-through cache
bookings/    Booking model + state machine, booking services, views
payments/    Payment, WebhookEvent, mock gateway, payment/webhook services, Celery tasks
tests/       pytest suite (API, services, state machine, concurrency)
scripts/     entrypoint, send_webhook.py (acts as the provider), demo_flow.py
```

**Layering:** views only parse and validate input (serializers), check permissions, and call a
**service function** (`bookings/services.py`, `payments/services.py`). All state changes and
transaction/locking logic live in the services, so the rules are in one place and can be tested
directly. Models own their invariants (state machine, DB constraints).

**Cross-cutting concerns**
* **Consistent errors.** Every error has the shape `{"error": {"code", "message", "details"}}` with a stable machine-readable `code` (`booking_not_payable`, `duplicate_booking`, `invalid_signature`, …).
* **Structured logging.** JSON lines to stdout. Each line carries the request's `X-Request-ID` (generated or propagated, and echoed in the response), so one request can be traced across logs.
* **Rate limiting.** DRF throttles backed by the shared cache (Redis): anon 60/min, user 300/min, auth endpoints 10/min, payments 20/min. All are configurable through env vars.
* **Caching.** Catalogue list/detail responses are cached in Redis using a version token. Any write to a centre, test or price bumps the version, which invalidates everything at once. If Redis is down, requests fall through to the DB.
* **Pagination.** Page-number pagination on all list endpoints (`?page=`, `?page_size=` up to 100).

---

## 3. Database design

```mermaid
erDiagram
    USER ||--o{ BOOKING : makes
    USER ||--o{ PAYMENT : makes
    DIAGNOSTIC_CENTRE ||--o{ CENTRE_TEST : offers
    DIAGNOSTIC_TEST ||--o{ CENTRE_TEST : "offered as"
    DIAGNOSTIC_CENTRE ||--o{ BOOKING : at
    DIAGNOSTIC_TEST ||--o{ BOOKING : for
    BOOKING ||--o{ PAYMENT : "paid by"
    PAYMENT ||--o{ WEBHOOK_EVENT : "updated by"

    USER { bigint id PK
           string email UK "stored lower-case + unique(lower(email))"
           string full_name
           string phone
           bool is_staff }
    DIAGNOSTIC_CENTRE { bigint id PK
           string name "unique (name, city)"
           string address
           string city "indexed"
           string pincode
           bool is_active "soft delete" }
    DIAGNOSTIC_TEST { bigint id PK
           string code UK "e.g. CBC"
           string name
           bool is_active "soft delete" }
    CENTRE_TEST { bigint id PK
           bigint centre_id FK "unique (centre, test)"
           bigint test_id FK
           decimal price "CHECK > 0"
           bool is_available }
    BOOKING { uuid id PK
           bigint user_id FK
           bigint centre_id FK
           bigint test_id FK
           datetime appointment_at
           decimal amount "price snapshot, CHECK > 0"
           string status "PENDING|CONFIRMED|FAILED|CANCELLED"
           datetime cancelled_at }
    PAYMENT { uuid id PK
           uuid booking_id FK
           bigint user_id FK
           decimal amount
           string status "PENDING|SUCCESS|FAILED"
           string provider_reference UK
           string idempotency_key "unique (user, key)"
           bool needs_refund }
    WEBHOOK_EVENT { bigint id PK
           string event_id UK "idempotency key of the webhook"
           string event_type
           string payment_reference
           json payload
           string status "RECEIVED|PROCESSED|REJECTED|FAILED"
           int attempts
           int response_status
           json response_body }
```

**Key decisions**

| Decision | Why |
|---|---|
| Price lives on `CentreTest` (centre × test), not on the test | The same test costs different amounts at different centres. |
| `Booking.amount` is a **snapshot** taken from the server-side price | Price changes don't affect existing bookings, and the client can never set the amount. |
| `Booking`/`Payment` IDs are **UUIDs** | They can't be enumerated (`/bookings/1/`, `/bookings/2/` …). |
| Partial unique index `unique_active_booking_per_slot` on `(user, centre, test, appointment_at) WHERE status IN (PENDING, CONFIRMED)` | Blocks double-click duplicate bookings even under races. Rebooking after cancel/failure still works. |
| Partial unique index `one_active_payment_per_booking` on `(booking) WHERE status IN (PENDING, SUCCESS)` | **At most one in-flight or successful payment per booking**, guaranteed by the DB. Rules out double charges. |
| Unique `(user, idempotency_key)` | Supports the `Idempotency-Key` header on `POST /payments/`. |
| Unique `WebhookEvent.event_id` | The core of webhook idempotency (see §5). |
| `CHECK` constraints on amounts/prices/status | Invariants hold even for writes that bypass the API. |
| `PROTECT` FKs + soft delete (`is_active`) for centres/tests | History is never orphaned. A booking always points to a real centre/test. |
| Indexes: `booking(user, -created_at)`, `booking(status)`, `centre(city)`, `webhook(status, received_at)` | Match the actual query patterns ("my bookings", filters, retry sweeper). |

### Booking state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING: booking created
    PENDING --> CONFIRMED: payment SUCCESS
    PENDING --> FAILED: payment FAILED
    PENDING --> CANCELLED: user cancels
    CONFIRMED --> CANCELLED: user cancels (payment flagged needs_refund)
    FAILED --> [*]
    CANCELLED --> [*]
```

Transitions are enforced in `Booking.transition_to()`. An illegal move raises `409 invalid_booking_transition`. There is no generic `PUT/PATCH/DELETE` on bookings, so status can't be set directly.

---

## 4. API reference & example requests

Base URL: `/api/v1`. Auth header: `Authorization: Bearer <access_token>`.
Full interactive docs: **`/api/docs/`**.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup/` | – | Create account → JWT pair + profile |
| POST | `/auth/login/` | – | Email + password → JWT pair + profile |
| POST | `/auth/token/refresh/` | – | Refresh → new access token |
| GET | `/auth/me/` | User | Current user's profile |
| GET | `/centres/?city=&test=&search=` | – | List centres (with tests and prices) |
| GET | `/centres/{id}/` | – | Centre detail |
| POST/PATCH/DELETE | `/centres/` · `/centres/{id}/` | Staff | Manage centres (DELETE = deactivate) |
| GET | `/centres/{id}/tests/` | – | Tests offered by a centre, with prices |
| POST/PATCH/DELETE | `/centres/{id}/tests/` · `/centres/{id}/tests/{offering_id}/` | Staff | Add a test to a centre / change price / remove |
| GET | `/tests/?search=` | – | Global test catalogue |
| POST/PATCH/DELETE | `/tests/` · `/tests/{id}/` | Staff | Manage tests |
| POST | `/bookings/` | User | Book a test |
| GET | `/bookings/?status=` | User | My bookings (staff: all) |
| GET | `/bookings/{id}/` | User | Booking detail (only own) |
| POST | `/bookings/{id}/cancel/` | User | Cancel a booking |
| POST | `/payments/` | User | Pay for a booking (simulated). Supports `Idempotency-Key` |
| GET | `/payments/` · `/payments/{id}/` | User | My payments |
| POST | `/payments/webhook/` | HMAC signature | Provider → us payment status updates |
| GET | `/health/` | – | Liveness / DB check |

### Examples (curl)

```bash
API=http://localhost:8000/api/v1

# Sign up (or log in)
curl -s -X POST $API/auth/signup/ -H 'Content-Type: application/json' \
  -d '{"email":"riya@example.com","password":"Str0ng-Passw0rd!","full_name":"Riya Sharma","phone":"+919876543210"}'

TOKEN=$(curl -s -X POST $API/auth/login/ -H 'Content-Type: application/json' \
  -d '{"email":"riya@example.com","password":"Str0ng-Passw0rd!"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access"])')
```

```jsonc
// 200 OK  POST /auth/login/
{ "refresh": "eyJ...", "access": "eyJ...",
  "user": { "id": 3, "email": "riya@example.com", "full_name": "Riya Sharma", "phone": "+919876543210", "date_joined": "..." } }
```

```bash
# Browse centres in a city that offer a given test
curl -s "$API/centres/?city=Gangtok&test=CBC"
```

```jsonc
{ "count": 1, "next": null, "previous": null,
  "results": [{ "id": 1, "name": "EVE Diagnostics - MG Marg", "address": "12 MG Marg", "city": "Gangtok",
                "pincode": "737101", "phone": "+913592200001", "is_active": true,
                "tests": [{ "id": 1, "test": {"id": 1, "code": "CBC", "name": "Complete Blood Count", ...},
                            "price": "350.00", "is_available": true }, ...] }] }
```

```bash
# Book a test (amount is computed server-side)
curl -s -X POST $API/bookings/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"centre_id":1,"test_id":1,"appointment_at":"2026-10-10T09:30:00+05:30"}'
```

```jsonc
// 201 Created
{ "id": "c0225011-4686-4892-9301-8ebaa935385e", "user_id": 3,
  "centre": {"id": 1, "name": "EVE Diagnostics - MG Marg", "city": "Gangtok", "address": "12 MG Marg"},
  "test": {"id": 1, "code": "CBC", "name": "Complete Blood Count"},
  "appointment_at": "2026-10-10T09:30:00+05:30", "amount": "350.00", "currency": "INR",
  "status": "PENDING", "created_at": "...", "updated_at": "...", "cancelled_at": null }
```

```bash
# Pay. simulate_outcome: SUCCESS (default) | FAILED | PENDING (result arrives via webhook)
curl -s -X POST $API/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-c0225011' \
  -d '{"booking_id":"c0225011-4686-4892-9301-8ebaa935385e","simulate_outcome":"SUCCESS"}'
```

```jsonc
// 201 Created (a retry with the same Idempotency-Key returns 200 + the same payment + header Idempotent-Replayed: true)
{ "id": "3b150230-...", "booking_id": "c0225011-...", "booking_status": "CONFIRMED",
  "amount": "350.00", "currency": "INR", "status": "SUCCESS", "provider": "mockpay",
  "provider_reference": "pay_mock_9f1c2e...", "failure_reason": "", "needs_refund": false,
  "created_at": "...", "completed_at": "..." }
```

```bash
# Webhook from the provider. Easiest via the helper, which signs the request:
python scripts/send_webhook.py --reference pay_mock_9f1c2e... --amount 350.00 --status success --event-id evt_123 --repeat 2

# ...or by hand:
BODY='{"event_id":"evt_123","type":"payment.succeeded","data":{"payment_reference":"pay_mock_9f1c2e...","amount":"350.00","currency":"INR"}}'
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$PAYMENT_WEBHOOK_SECRET" -hex | sed 's/^.* //')
curl -s -X POST $API/payments/webhook/ -H 'Content-Type: application/json' \
  -H "X-Webhook-Timestamp: $TS" -H "X-Webhook-Signature: $SIG" -d "$BODY"
```

```jsonc
// 200 OK (first delivery). Redeliveries return the same body with "duplicate": true
{ "event_id": "evt_123", "status": "processed", "outcome": "applied",
  "payment_id": "...", "payment_status": "SUCCESS", "booking_id": "...", "booking_status": "CONFIRMED",
  "duplicate": false }
```

```bash
# Cancel
curl -s -X POST $API/bookings/c0225011-.../cancel/ -H "Authorization: Bearer $TOKEN"
```

```jsonc
// Example error (409)
{ "error": { "code": "booking_not_payable", "message": "Booking is CONFIRMED and cannot be paid.",
             "details": { "booking_status": "CONFIRMED" } } }
```

---

## 5. Payments & the idempotent webhook

### Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant API
    participant DB
    participant P as MockPay (provider)
    C->>API: POST /payments/ {booking_id} + Idempotency-Key
    API->>DB: BEGIN; lock booking row; check PENDING, not expired, no in-flight payment
    API->>DB: INSERT payment (PENDING); COMMIT
    API->>P: charge() (outside the transaction: never hold locks across network calls)
    alt provider answers SUCCESS / FAILED
        API->>DB: apply_payment_result(): lock booking → payment, update both
    else provider answers PENDING
        P-->>API: later: POST /payments/webhook/ (signed)
        API->>DB: store event by event_id, lock it, apply_payment_result()
    end
    API-->>C: payment + booking status
```

### Simulated payment (`POST /payments/`)
* The amount always comes from the booking. The client can't send one.
* `simulate_outcome` makes the mock gateway deterministic: `SUCCESS` → booking `CONFIRMED`, `FAILED` → booking `FAILED`, `PENDING` → both stay pending until a webhook arrives (the realistic async case).
* **Client idempotency:** an optional `Idempotency-Key` header. A retry with the same key returns the original payment (`200`, `Idempotent-Replayed: true`) and never charges again. Reusing the key for a different booking → `422 idempotency_key_reused`.
* **No double charge:** the booking row is locked (`SELECT … FOR UPDATE`) and the partial unique index `one_active_payment_per_booking` backs this up. Concurrent pay requests for one booking produce exactly one payment, and the others get `409 payment_in_progress` (covered by `tests/test_concurrency.py`).

### Webhook (`POST /payments/webhook/`)

**Authentication.** The provider signs `"{timestamp}.{raw_body}"` with HMAC-SHA256 using a shared secret (`PAYMENT_WEBHOOK_SECRET`) and sends `X-Webhook-Timestamp` and `X-Webhook-Signature`. We verify the signature against the raw bytes with a constant-time compare, *before* parsing or trusting anything. Timestamps older than 5 minutes are rejected, which blocks replay attacks. Unsigned, forged or tampered requests → `401`, and nothing is stored.

**Idempotency, in three layers:**
1. **Event de-duplication.** Each event is stored once in `WebhookEvent`, keyed by the provider's unique `event_id`. A redelivery finds the existing row.
2. **Serialisation.** Processing locks the event row (`SELECT … FOR UPDATE`). Concurrent deliveries of the same event queue behind each other. The first one applies it. The rest see a final status and get back the **stored original response** with `"duplicate": true`, so the same request always gets the same answer.
3. **State guards.** Even *different* events about the same payment can't corrupt state. Only a `PENDING` payment or booking can move. The same result again → `outcome: already_applied`. A conflicting late result (e.g. *failed* after *succeeded*) → `outcome: ignored_payment_already_final` and a warning log. Final states never flip.

**Validation against our records.** An unknown `payment_reference` → `404 rejected/unknown_payment`. An amount or currency that doesn't match the payment → `422 rejected/amount_mismatch`, with no state change. Both outcomes are recorded and replayed the same way on redelivery.

**Failures and retries.**
* If processing throws (e.g. a DB hiccup), the transaction rolls back cleanly. The event is marked `FAILED` with `attempts` and `last_error`, and the API answers `503` so the provider redelivers.
* A Celery task is also queued with exponential backoff (30s, 60s, 120s … max 1h, up to `WEBHOOK_MAX_ATTEMPTS`).
* A Celery-beat job (`reprocess_failed_webhook_events`, every 5 min) and `python manage.py reprocess_webhooks` sweep up anything left `FAILED` or stuck in `RECEIVED`.
* Every one of these paths goes through the same idempotent processor, so retries racing each other is harmless.

**Lock ordering.** Locks are always taken in the order *webhook event → booking → payment*, which avoids deadlocks between cancels, payments and webhooks.

**Cancel vs. payment race.** If a booking is cancelled while its payment is pending and the provider then reports success, the payment becomes `SUCCESS` but the booking stays `CANCELLED`, and the payment is flagged `needs_refund=true`. Cancelling a `CONFIRMED` booking flags its payment the same way.

---

## 6. Edge cases handled

| Case | Behaviour |
|---|---|
| Invalid / missing fields, bad email, weak or common password, bad phone | `400 validation_error` with per-field `details` |
| Duplicate email (any letter case, incl. concurrent signups) | `400` (unique index on `lower(email)` as the backstop) |
| Wrong credentials / inactive user / bad or expired JWT | `401` |
| Login brute force / API abuse | `429` (scoped throttles) |
| Non-staff tries to modify the catalogue | `403` (anonymous: `401`) |
| Booking in the past / > 60 days ahead | `400` |
| Test not offered (or unavailable) at the chosen centre; inactive or non-existent centre/test | `400` |
| Duplicate booking of the same slot (incl. concurrent double-click) | `409 duplicate_booking` |
| Client sends `amount`/`status` when booking | Ignored. Price comes from the server, status always starts at `PENDING` |
| Invalid / malformed / someone else's booking ID | `404` (no information leak about other users' bookings) |
| Direct `PATCH`/`DELETE` of a booking | `405`. Status changes only through the state machine |
| Cancel an already cancelled / failed booking | `409 invalid_booking_transition` |
| Pay a confirmed / failed / cancelled booking | `409 booking_not_payable` |
| Pay while another payment is in flight; concurrent pay requests | `409 payment_in_progress`. Exactly one payment |
| Pay for an appointment time that has passed | `409 booking_expired` |
| Client retries `POST /payments/` | Same `Idempotency-Key` → original payment, no second charge |
| Failed payment | Payment `FAILED` with `failure_reason`, booking `FAILED` |
| Webhook: missing / forged / tampered / stale signature | `401`, nothing stored |
| Webhook: malformed payload, unknown event type | `400` |
| Webhook: same event delivered N times (incl. concurrently) | Applied once. Same response each time with `duplicate: true` |
| Webhook: conflicting or out-of-order event | Ignored. Final states never flip |
| Webhook: unknown payment reference / amount mismatch | `404` / `422`, no state change |
| Webhook: transient processing error | Rolled back, recorded, `503`, retried with backoff |
| Payment succeeds for a cancelled booking | Booking stays cancelled, payment `needs_refund=true` |
| Catalogue price changes after booking | Booking keeps its snapshot price |
| Redis unavailable for the catalogue cache | Cache errors are caught and logged, and the response is served from the DB |

---

## 7. Testing

```bash
pytest                        # 117 pass + 3 concurrency tests skipped on SQLite
pytest --cov                  # coverage report (~95%)
DATABASE_URL=postgres://eve:eve@localhost:5432/eve pytest   # all 120, incl. concurrency tests
docker compose exec web pytest                              # same, inside Docker
```

| File | What it covers |
|---|---|
| `test_auth.py` | signup/login/refresh/me, validation, duplicate email, inactive user, throttling |
| `test_catalog.py` | public reads, filters, staff-only writes, price validation, soft delete, cache hit/invalidation, pagination |
| `test_bookings.py` | creation and price snapshot, all validation paths, ownership isolation, duplicates, cancel rules, refund flag |
| `test_payments.py` | success/failure/pending, ownership, non-payable states, expiry, `Idempotency-Key` replay and misuse |
| `test_webhooks.py` | signature (missing/wrong/tampered/stale), malformed payloads, duplicate delivery, conflicting events, unknown payment, amount mismatch, transient failure + retry + backoff + max attempts, sweeper |
| `test_state_machine.py` | every (from, to) pair of the booking state machine |
| `test_concurrency.py` | **real threads on PostgreSQL**: 8 simultaneous identical webhooks → applied once; 8 simultaneous pay requests → one payment; 8 identical bookings → one booking |
| `test_common.py` | health check, request-ID propagation, error envelope, JSON log format, OpenAPI schema |

The concurrency tests were checked by mutation. Removing the `SELECT … FOR UPDATE` on the webhook event makes `test_concurrent_duplicate_webhooks_apply_once` fail (8 of 8 deliveries applied), so the test genuinely exercises the locking.

CI (`.github/workflows/ci.yml`) runs lint (ruff), a migrations-up-to-date check, OpenAPI schema validation and the full suite with coverage against PostgreSQL 16.

---

## 8. Assumptions

* **One test per booking.** A patient wanting several tests creates several bookings. A cart/order model is a natural extension.
* **FAILED and CANCELLED are terminal.** After a failed payment the patient creates a new booking instead of retrying on the failed one. This keeps the state machine and the audit trail simple, and the partial unique index lets them rebook the same slot right away.
* **Only `PENDING` bookings can be paid**, and only before the appointment time.
* **Centre capacity / time slots are not modelled.** Any future time within 60 days is accepted. Appointment times are stored in UTC and shown in `Asia/Kolkata`. Naive datetimes are treated as IST.
* **Single currency (INR).** Amounts are `Decimal(10,2)`, never floats.
* **The mock provider is part of this service.** `simulate_outcome` chooses its answer. In production this would be a Razorpay/Stripe client, and the webhook format/signature scheme mirrors theirs (`timestamp.body` HMAC).
* **Refunds are flagged, not executed** (`needs_refund=true`). Issuing refunds would be a separate provider integration.
* **Catalogue management is staff-only** (`is_staff`). Patients and anonymous users can read the catalogue. Bookings and payments are private to their owner, and staff can view all.
* **Webhook endpoint authentication is by HMAC signature only**, not by user JWT, as with real providers.

---

## 9. What I would improve with more time

* **Slot / capacity management:** centre opening hours and per-slot capacity with `SELECT … FOR UPDATE` on a slot row so a centre can't be overbooked.
* **Expire stale PENDING bookings:** a periodic task that cancels unpaid bookings after N minutes and frees the slot.
* **Transactional outbox:** emit domain events (booking confirmed, payment failed) for notifications (SMS/email) reliably.
* **Refund flow:** actually call the provider's refund API for `needs_refund` payments, with its own webhook events.
* **Payment retries on the same booking** (multiple attempts per booking) instead of terminal FAILED, if product wants it. The schema already allows several FAILED payments per booking.
* **Webhook secret rotation** (accept two secrets during rotation) and an allow-list of provider IPs.
* **Observability:** Prometheus metrics (webhook lag, failure rate), OpenTelemetry tracing, Sentry.
* **Auth hardening:** refresh-token rotation + blacklist on logout, email verification, password reset.
* **Load testing** of the booking/payment hot paths (Locust) and DB query-count assertions in tests.

---

*Author: Asmit Pandey*
