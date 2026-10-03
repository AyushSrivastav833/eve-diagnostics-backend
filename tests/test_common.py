import json
import logging

import pytest

from common.logging import JSONFormatter, RequestIDFilter, request_id_var

pytestmark = pytest.mark.django_db


def test_health_check(api_client):
    response = api_client.get("/health/")
    assert response.status_code == 200
    assert response.data == {"status": "ok", "database": True}


def test_request_id_is_generated_and_echoed(api_client):
    generated = api_client.get("/health/")
    assert len(generated["X-Request-ID"]) == 32

    echoed = api_client.get("/health/", HTTP_X_REQUEST_ID="trace-abc-123")
    assert echoed["X-Request-ID"] == "trace-abc-123"


def test_malicious_request_id_is_replaced(api_client):
    response = api_client.get("/health/", HTTP_X_REQUEST_ID="bad id\nwith newline")
    assert response["X-Request-ID"] != "bad id\nwith newline"


def test_errors_use_consistent_envelope(api_client):
    response = api_client.get("/api/v1/bookings/")
    assert response.status_code == 401
    assert set(response.data["error"]) == {"code", "message", "details"}


def test_openapi_schema_is_served(api_client):
    response = api_client.get("/api/schema/", HTTP_ACCEPT="application/json")
    assert response.status_code == 200


def test_json_log_formatter_includes_request_id_and_extras():
    record = logging.LogRecord("eve", logging.INFO, __file__, 1, "booking_created", (), None)
    record.booking_id = "b-1"
    token = request_id_var.set("req-1")
    try:
        RequestIDFilter().filter(record)
    finally:
        request_id_var.reset(token)

    line = json.loads(JSONFormatter().format(record))
    assert line["event"] == "booking_created"
    assert line["request_id"] == "req-1"
    assert line["booking_id"] == "b-1"
    assert line["level"] == "INFO"
