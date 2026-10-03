from decimal import Decimal

import pytest

from catalog.models import CentreTest, DiagnosticCentre, DiagnosticTest

pytestmark = pytest.mark.django_db


def test_centres_are_public_and_include_tests_with_prices(api_client, offering):
    response = api_client.get("/api/v1/centres/")
    assert response.status_code == 200
    assert response.data["count"] == 1
    centre = response.data["results"][0]
    assert centre["name"] == offering.centre.name
    assert centre["tests"] == [
        {
            "id": offering.id,
            "test": {
                "id": offering.test.id,
                "code": "CBC",
                "name": "Complete Blood Count",
                "description": "",
                "is_active": True,
            },
            "price": "499.00",
            "is_available": True,
        }
    ]


def test_filter_centres_by_city_and_test(api_client, offering):
    other = DiagnosticCentre.objects.create(name="EVE Delhi", address="Saket", city="New Delhi", pincode="110017")
    lipid = DiagnosticTest.objects.create(code="LIPID", name="Lipid Profile")
    CentreTest.objects.create(centre=other, test=lipid, price=Decimal("650"))

    by_city = api_client.get("/api/v1/centres/", {"city": "gangtok"}).data
    assert [c["name"] for c in by_city["results"]] == ["EVE Gangtok"]

    by_test = api_client.get("/api/v1/centres/", {"test": "lipid"}).data
    assert [c["name"] for c in by_test["results"]] == ["EVE Delhi"]


def test_inactive_and_unavailable_items_are_hidden_from_patients(api_client, offering):
    offering.is_available = False
    offering.save()
    centre = api_client.get(f"/api/v1/centres/{offering.centre_id}/").data
    assert centre["tests"] == []

    offering.centre.is_active = False
    offering.centre.save()
    assert api_client.get(f"/api/v1/centres/{offering.centre_id}/").status_code == 404


def test_patients_cannot_modify_catalogue(auth_client, centre):
    assert auth_client.post("/api/v1/centres/", {"name": "X"}, format="json").status_code == 403
    assert auth_client.patch(f"/api/v1/centres/{centre.id}/", {"name": "X"}, format="json").status_code == 403
    assert auth_client.post("/api/v1/tests/", {"code": "X", "name": "X"}, format="json").status_code == 403


def test_anonymous_cannot_modify_catalogue(api_client):
    assert api_client.post("/api/v1/tests/", {"code": "X", "name": "X"}, format="json").status_code == 401


def test_staff_manages_centres_tests_and_prices(staff_client):
    centre = staff_client.post(
        "/api/v1/centres/",
        {"name": "EVE Pune", "address": "FC Road", "city": "Pune", "pincode": "411004"},
        format="json",
    )
    assert centre.status_code == 201

    test = staff_client.post("/api/v1/tests/", {"code": "tsh", "name": "Thyroid"}, format="json")
    assert test.status_code == 201
    assert test.data["code"] == "TSH"

    offering = staff_client.post(
        f"/api/v1/centres/{centre.data['id']}/tests/", {"test_id": test.data["id"], "price": "450.00"}, format="json"
    )
    assert offering.status_code == 201
    assert offering.data["price"] == "450.00"

    updated = staff_client.patch(
        f"/api/v1/centres/{centre.data['id']}/tests/{offering.data['id']}/", {"price": "475.50"}, format="json"
    )
    assert updated.status_code == 200
    assert CentreTest.objects.get(pk=offering.data["id"]).price == Decimal("475.50")


@pytest.mark.parametrize("price", ["0", "-10", "abc"])
def test_offering_price_must_be_positive_number(staff_client, centre, test_cbc, price):
    response = staff_client.post(
        f"/api/v1/centres/{centre.id}/tests/", {"test_id": test_cbc.id, "price": price}, format="json"
    )
    assert response.status_code == 400
    assert "price" in response.data["error"]["details"]


def test_cannot_offer_same_test_twice_at_a_centre(staff_client, offering):
    response = staff_client.post(
        f"/api/v1/centres/{offering.centre_id}/tests/", {"test_id": offering.test_id, "price": "10"}, format="json"
    )
    assert response.status_code == 400
    assert "test_id" in response.data["error"]["details"]


def test_duplicate_test_code_is_rejected_case_insensitively(staff_client, test_cbc):
    response = staff_client.post("/api/v1/tests/", {"code": "cbc", "name": "Again"}, format="json")
    assert response.status_code == 400


def test_delete_centre_soft_deletes(staff_client, api_client, centre):
    assert staff_client.delete(f"/api/v1/centres/{centre.id}/").status_code == 204
    centre.refresh_from_db()
    assert centre.is_active is False
    assert api_client.get(f"/api/v1/centres/{centre.id}/").status_code == 404


def test_unknown_centre_returns_404(api_client):
    assert api_client.get("/api/v1/centres/999999/").status_code == 404
    assert api_client.get("/api/v1/centres/999999/tests/").status_code == 404


def test_catalogue_responses_are_cached_and_invalidated_on_write(api_client, staff_client, offering):
    first = api_client.get("/api/v1/centres/")
    second = api_client.get("/api/v1/centres/")
    assert first["X-Cache"] == "MISS"
    assert second["X-Cache"] == "HIT"
    assert second.data == first.data

    staff_client.patch(f"/api/v1/centres/{offering.centre_id}/tests/{offering.id}/", {"price": "999.00"}, format="json")
    fresh = api_client.get("/api/v1/centres/")
    assert fresh["X-Cache"] == "MISS"
    assert fresh.data["results"][0]["tests"][0]["price"] == "999.00"


def test_list_is_paginated(api_client, db):
    for i in range(25):
        DiagnosticCentre.objects.create(name=f"Centre {i:02d}", address="a", city="Gangtok", pincode="737101")
    page1 = api_client.get("/api/v1/centres/").data
    assert page1["count"] == 25
    assert len(page1["results"]) == 20
    assert page1["next"] is not None
    page2 = api_client.get("/api/v1/centres/", {"page": 2}).data
    assert len(page2["results"]) == 5
