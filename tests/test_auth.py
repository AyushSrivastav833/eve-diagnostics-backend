import pytest
from rest_framework.throttling import ScopedRateThrottle

from accounts.models import User
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db

SIGNUP_URL = "/api/v1/auth/signup/"
LOGIN_URL = "/api/v1/auth/login/"
ME_URL = "/api/v1/auth/me/"


def test_signup_creates_user_and_returns_tokens(api_client):
    response = api_client.post(
        SIGNUP_URL, {"email": "New.User@Example.com", "password": PASSWORD, "full_name": "New User"}, format="json"
    )
    assert response.status_code == 201
    assert {"access", "refresh", "user"} <= response.data.keys()
    assert response.data["user"]["email"] == "new.user@example.com"  # normalised
    assert "password" not in response.data["user"]
    assert User.objects.get(email="new.user@example.com").check_password(PASSWORD)


def test_signup_rejects_duplicate_email_case_insensitively(api_client, user):
    response = api_client.post(
        SIGNUP_URL, {"email": user.email.upper(), "password": PASSWORD, "full_name": "Dup"}, format="json"
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert "email" in response.data["error"]["details"]


@pytest.mark.parametrize(
    "payload, bad_field",
    [
        ({"email": "not-an-email", "password": PASSWORD, "full_name": "X"}, "email"),
        ({"email": "a@b.com", "password": "123", "full_name": "X"}, "password"),
        ({"email": "a@b.com", "password": "password123", "full_name": "X"}, "password"),
        ({"email": "a@b.com", "password": PASSWORD}, "full_name"),
        ({"email": "a@b.com", "password": PASSWORD, "full_name": "X", "phone": "abc"}, "phone"),
    ],
)
def test_signup_validation(api_client, payload, bad_field):
    response = api_client.post(SIGNUP_URL, payload, format="json")
    assert response.status_code == 400
    assert bad_field in response.data["error"]["details"]


def test_login_returns_jwt_pair(api_client, user):
    response = api_client.post(LOGIN_URL, {"email": "  PATIENT@example.com ", "password": PASSWORD}, format="json")
    assert response.status_code == 200
    assert response.data["user"]["email"] == user.email

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    me = api_client.get(ME_URL)
    assert me.status_code == 200
    assert me.data["email"] == user.email


def test_login_with_wrong_password_is_rejected(api_client, user):
    response = api_client.post(LOGIN_URL, {"email": user.email, "password": "wrong-password"}, format="json")
    assert response.status_code == 401
    assert "error" in response.data


def test_inactive_user_cannot_login(api_client, user):
    user.is_active = False
    user.save()
    response = api_client.post(LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json")
    assert response.status_code == 401


def test_refresh_token_issues_new_access_token(api_client, user):
    tokens = api_client.post(LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json").data
    response = api_client.post("/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]}, format="json")
    assert response.status_code == 200
    assert "access" in response.data


def test_me_requires_authentication(api_client):
    response = api_client.get(ME_URL)
    assert response.status_code == 401
    assert response.data["error"]["code"] == "not_authenticated"


def test_garbage_token_is_rejected(api_client):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not.a.jwt")
    assert api_client.get(ME_URL).status_code == 401


def test_login_is_rate_limited(api_client, user, monkeypatch):
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "3/min"})
    statuses = [
        api_client.post(LOGIN_URL, {"email": user.email, "password": "wrong"}, format="json").status_code
        for _ in range(4)
    ]
    assert statuses == [401, 401, 401, 429]
