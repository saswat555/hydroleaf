# tests/test_auth.py
import pytest
from httpx import AsyncClient
from app.main import app
from jose import jwt

# Base URL for auth endpoints
AUTH_URL = "/api/v1/auth"

@pytest.fixture
async def signed_up_user(async_client: AsyncClient):
    """
    Creates a user via the signup endpoint and returns (payload, token, headers).
    """
    payload = {
        "email": "user@example.com",
        "password": "StrongPass123!",
        "first_name": "John",
        "last_name": "Doe",
        "phone": "1234567890",
        "address": "123 Main St",
        "city": "Bangalore",
        "state": "KA",
        "country": "India",
        "postal_code": "560001",
    }
    resp = await async_client.post(f"{AUTH_URL}/signup", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return payload, token, headers

@pytest.mark.asyncio
async def test_signup_success(async_client: AsyncClient):
    payload = {
        "email": "newuser@example.com",
        "password": "AnotherPass!1",
        "profile": {
            "first_name": "Alice",
            "last_name": "Smith",
            "phone": "9876543210",
            "address": "456 Side St",
            "city": "Delhi",
            "state": "DL",
            "country": "India",
            "postal_code": "110001",
        },
    }
    resp = await async_client.post(f"{AUTH_URL}/signup", json=payload)
    print(resp)
    assert resp.status_code == 201
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    user = data["user"]
    assert user["email"] == payload["email"]
    assert user["profile"]["first_name"] == payload["first_name"]

@pytest.mark.asyncio
async def test_signup_duplicate_email(async_client: AsyncClient, signed_up_user):
    payload, token, headers = signed_up_user
    resp = await async_client.post(f"{AUTH_URL}/signup", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Email already registered"

@pytest.mark.asyncio
async def test_signup_invalid_email_format(async_client: AsyncClient):
    payload = {
        "email": "not-an-email",
        "password": "Pass123!",
        "profile": {
            "first_name": "I",
            "last_name": "N",
            "phone": "1112223333",
            "address": "X St",
            "city": "Y",
            "state": "Z",
            "country": "C",
            "postal_code": "000000",
        },
    }
    resp = await async_client.post(f"{AUTH_URL}/signup", json=payload)
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_signup_missing_fields(async_client: AsyncClient):
    # Missing all profile fields
    resp = await async_client.post(f"{AUTH_URL}/signup", json={"email": "a@b.com"})
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_signup_extra_field(async_client: AsyncClient):
    payload = {
        "email": "extra@example.com",
        "password": "Pass123!",
        "profile": {
            "first_name": "E",
            "last_name": "X",
            "phone": "0000000000",
            "address": "Addr",
            "city": "City",
            "state": "ST",
            "country": "CO",
            "postal_code": "123456",
        },
        "unexpected": "value",   # still checking extra-field rejection
    }
    resp = await async_client.post(f"{AUTH_URL}/signup", json=payload)
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_login_success(async_client: AsyncClient, signed_up_user):
    payload, token, headers = signed_up_user
    form = {"username": payload["email"], "password": payload["password"]}
    resp = await async_client.post(
        f"{AUTH_URL}/login",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == payload["email"]

@pytest.mark.asyncio
async def test_login_invalid_password(async_client: AsyncClient, signed_up_user):
    payload, _, _ = signed_up_user
    form = {"username": payload["email"], "password": "wrongpass"}
    resp = await async_client.post(
        f"{AUTH_URL}/login",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"

@pytest.mark.asyncio
async def test_login_nonexistent_user(async_client: AsyncClient):
    form = {"username": "nouser@example.com", "password": "nopass"}
    resp = await async_client.post(
        f"{AUTH_URL}/login",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"

@pytest.mark.asyncio
async def test_login_missing_fields(async_client: AsyncClient):
    # Missing password
    resp = await async_client.post(
        f"{AUTH_URL}/login",
        data={"username": "user@example.com"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_token_payload_contains_user_and_role(async_client: AsyncClient, signed_up_user):
    _, token, _ = signed_up_user
    # decode without verifying signature
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded.get("user_id") is not None
    assert decoded.get("role") == "user"