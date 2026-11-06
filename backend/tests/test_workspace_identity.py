"""Identity must select a workspace before any tenant database operation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from google.auth.exceptions import TransportError

from rubik_core.config import AppMode, settings
from rubik_core.tenancy import context


@pytest.fixture
def isolated_control_db(tmp_path, monkeypatch):
    from rubik_core.control import db as control_db
    from rubik_core.control.models import Base as ControlBase

    previous = control_db._engine
    with monkeypatch.context() as patch:
        patch.setattr(
            settings, "CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
        )
        control_db._engine = None
        engine = control_db.get_engine()
        ControlBase.metadata.create_all(engine)
        try:
            yield engine
        finally:
            engine.dispose()
            control_db._engine = previous


def test_unbound_engine_refuses_a_server_default(monkeypatch):
    from rubik_core.database import db

    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.setattr(db, "_engine", None)
    with pytest.raises(context.NoWorkspaceContext):
        db.get_engine()


def test_session_factory_uses_current_workspace_engine(
    tmp_path, monkeypatch, isolated_control_db
):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace
    from rubik_core.database import db
    from rubik_core.tenancy.context import use_workspace

    with control_session() as session:
        session.merge(Workspace(slug="identity-a", name="Identity A", status="active"))
        session.commit()
    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path))
    with use_workspace("identity-a"):
        first = db.get_engine()
        session = db.SessionLocal()
        try:
            assert session.get_bind() is first
        finally:
            session.close()
    with use_workspace("test"):
        second = db.get_engine()
    assert first is not second
    assert first.url.database != second.url.database


def test_session_claims_require_workspace_and_audience(monkeypatch):
    from rubik_core.auth import session_tokens
    from rubik_core.tenancy.context import use_workspace

    monkeypatch.setattr(session_tokens, "session_lifetime_hours", lambda: 1.0)
    with use_workspace("test"):
        token, _, _ = session_tokens.mint_session_token("pat@acme.example", "test")
    claims = session_tokens.decode_session_token(token)
    assert claims is not None
    assert claims.sub == "pat@acme.example"
    assert claims.ws == "test"
    assert claims.aud == "cubie-session"
    assert claims.hd == "acme.example"

    key = session_tokens._signing_secret()
    for payload in (
        {
            "sub": "pat@acme.example",
            "iss": "cubie",
            "aud": "cubie-session",
            "exp": 2**31,
        },
        {"sub": "pat@acme.example", "iss": "cubie", "ws": "test", "exp": 2**31},
        {
            "sub": "pat@acme.example",
            "iss": "cubie",
            "aud": "cubie-session",
            "ws": "test",
            "typ": "user",
            "exp": 2**31,
        },
    ):
        legacy = jwt.encode(payload, key, algorithm="HS256")
        assert session_tokens.decode_session_token(legacy) is None


def test_google_identity_requires_verified_hosted_domain(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cubie-client")
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cubie-client",
        "email": "pat@alias.example",
        "hd": "acme.example",
        "sub": "google-sub",
        "email_verified": True,
        "exp": 2**31,
    }
    verifier = Mock(return_value=claims)
    monkeypatch.setattr(google_id_token, "_verify_google_token", verifier)
    identity = google_id_token.verify_google_id_token("signed-id-token")
    assert identity.email == "pat@alias.example"
    assert identity.hd == "acme.example"
    verifier.assert_called_once_with("signed-id-token", "cubie-client")

    for change in ({"email_verified": False}, {"hd": ""}, {"aud": "other-client"}):
        verifier.return_value = {**claims, **change}
        with pytest.raises(google_id_token.GoogleIdentityError):
            google_id_token.verify_google_id_token("signed-id-token")


def test_google_id_token_checks_real_signature_audience_and_bounded_certs(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cubie-client")
    monkeypatch.setattr(google_id_token, "_cert_cache", None)
    monkeypatch.setattr(google_id_token, "_rotation_retry_after", 0.0)
    monkeypatch.setattr(google_id_token, "_cert_failure_retry_after", 0.0)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cert_response = SimpleNamespace(
        status=200,
        headers={"Cache-Control": "max-age=60"},
        data=json.dumps({"test-key": public_pem.decode()}).encode(),
    )
    transports = []

    def fetch_certificates(request, url, **kwargs):
        transports.append((url, kwargs))
        return cert_response

    monkeypatch.setattr(google_id_token.GoogleRequest, "__call__", fetch_certificates)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cubie-client",
        "email": "pat@alias.example",
        "hd": "acme.example",
        "sub": "google-sub",
        "email_verified": True,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }

    def signed_token(key, payload):
        return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})

    identity = google_id_token.verify_google_id_token(signed_token(private_key, claims))
    assert (identity.email, identity.hd) == ("pat@alias.example", "acme.example")
    for key, payload in (
        (other_key, claims),
        (private_key, {**claims, "aud": "wrong-client"}),
    ):
        with pytest.raises(google_id_token.GoogleIdentityError) as refused:
            google_id_token.verify_google_id_token(signed_token(key, payload))
        assert refused.value.code == "invalid_google_token"
    assert len(transports) == 1
    assert transports[0][0] == google_id_token._CERT_URL
    assert transports[0][1]["timeout"] == 5


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.ConnectionError("cert endpoint offline"),
        TransportError("cert endpoint offline"),
    ],
)
def test_google_cert_transport_outage_is_temporary_service_error(monkeypatch, failure):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cubie-client")
    monkeypatch.setattr(
        google_id_token, "_verify_google_token", Mock(side_effect=failure)
    )
    with pytest.raises(google_id_token.GoogleIdentityError) as unavailable:
        google_id_token.verify_google_id_token("signed-id-token")
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "google_unavailable"


def test_meet_access_token_exchange_checks_online_audience_and_hd(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cubie-client")
    requests_seen = []

    def google_get(url, **kwargs):
        requests_seen.append((url, kwargs))
        if url.endswith("/userinfo"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "email": "pat@alias.example",
                    "email_verified": True,
                    "hd": "acme.example",
                    "sub": "g-1",
                },
            )
        return SimpleNamespace(status_code=200, json=lambda: {"aud": "cubie-client"})

    monkeypatch.setattr(google_id_token.requests, "get", google_get)
    identity = google_id_token.verify_google_access_token("meet-access-token")
    assert (identity.email, identity.hd) == ("pat@alias.example", "acme.example")
    assert [call[1]["timeout"] for call in requests_seen] == [10, 10]
    assert requests_seen[0][1]["headers"]["Authorization"] == "Bearer meet-access-token"
    assert requests_seen[1][1]["params"]["access_token"] == "meet-access-token"


def test_meet_access_token_exchange_reports_provider_outage(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cubie-client")
    monkeypatch.setattr(
        google_id_token.requests,
        "get",
        Mock(side_effect=requests.exceptions.ConnectionError("Google unavailable")),
    )
    with pytest.raises(google_id_token.GoogleIdentityError) as unavailable:
        google_id_token.verify_google_access_token("meet-access-token")
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "google_unavailable"


def test_google_cert_cache_expires_and_refreshes_for_new_kid(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(google_id_token, "_cert_cache", None)
    monkeypatch.setattr(google_id_token, "_rotation_retry_after", 0.0)
    monkeypatch.setattr(google_id_token, "_cert_failure_retry_after", 0.0)
    fetches = []

    def fetch_certificates(request, url, **kwargs):
        fetches.append(url)
        return SimpleNamespace(
            status=200,
            headers={"Cache-Control": "max-age=60"},
            data=json.dumps({f"key-{len(fetches)}": "public-pem"}).encode(),
        )

    monkeypatch.setattr(google_id_token.GoogleRequest, "__call__", fetch_certificates)
    request = google_id_token._BoundedGoogleRequest()
    assert request(google_id_token._CERT_URL).status == 200
    assert request(google_id_token._CERT_URL).status == 200
    assert len(fetches) == 1

    expires_at, cached = google_id_token._cert_cache
    monkeypatch.setattr(google_id_token, "_cert_cache", (expires_at - 3600, cached))
    assert request(google_id_token._CERT_URL).status == 200
    assert len(fetches) == 2

    rotated_token = jwt.encode(
        {"exp": 2**31}, "t" * 32, algorithm="HS256", headers={"kid": "key-3"}
    )
    google_id_token._refresh_for_new_key(rotated_token)
    assert request(google_id_token._CERT_URL).status == 200
    assert len(fetches) == 3


def test_google_cert_fetch_failure_backoff_coalesces_concurrent_sign_ins(monkeypatch):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(google_id_token, "_cert_cache", None)
    monkeypatch.setattr(google_id_token, "_cert_failure_retry_after", 0.0)
    fetches = []
    start = Barrier(4)

    def failing_fetch(request, url, **kwargs):
        fetches.append(url)
        time.sleep(0.1)
        raise requests.exceptions.ConnectionError("Google unavailable")

    monkeypatch.setattr(google_id_token.GoogleRequest, "__call__", failing_fetch)

    def attempt():
        start.wait(timeout=3)
        with pytest.raises(requests.exceptions.ConnectionError):
            google_id_token._BoundedGoogleRequest()(google_id_token._CERT_URL)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(attempt) for _ in range(3)]
        start.wait(timeout=3)
        for future in futures:
            future.result(timeout=3)
    assert len(fetches) == 1
    monkeypatch.setattr(google_id_token, "_cert_failure_retry_after", 0.0)
    with pytest.raises(requests.exceptions.ConnectionError):
        google_id_token._BoundedGoogleRequest()(google_id_token._CERT_URL)
    assert len(fetches) == 2


def test_opaque_bearer_rejected_before_tenant_db(monkeypatch):
    from backend.main import app
    from rubik_core.database import db

    lookups = Mock(side_effect=AssertionError("tenant DB must not be touched"))
    monkeypatch.setattr(db, "engine_for", lookups)
    response = TestClient(app).get(
        "/api/users/me", headers={"Authorization": "Bearer opaque-google-token"}
    )
    assert response.status_code == 401
    lookups.assert_not_called()


@pytest.mark.parametrize("row", [None, SimpleNamespace(slug="other", status="active")])
def test_removed_or_reassigned_hosted_domain_refuses_before_tenant_db(monkeypatch, row):
    from backend.main import app
    from rubik_core.auth.session_tokens import mint_session_token
    from rubik_core.database import db
    from rubik_core.tenancy import middleware
    from rubik_core.tenancy.context import use_workspace

    lookups = Mock(side_effect=AssertionError("tenant DB must not be touched"))
    monkeypatch.setattr(db, "engine_for", lookups)
    control_lookup = Mock(return_value=row)
    monkeypatch.setattr(middleware, "workspace_for_domain_any", control_lookup)
    with use_workspace("acme"):
        token = mint_session_token(
            "pat@alias.example", "acme", hd="acme.example", hours=1.0
        )[0]
    response = TestClient(app).get(
        "/api/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "workspace_unavailable"
    control_lookup.assert_called_once_with("acme.example")
    lookups.assert_not_called()


def test_unhandled_error_never_exposes_secret_even_when_cloud_run_mode_is_local(
    monkeypatch,
):
    from backend.main import catch_exceptions_middleware

    monkeypatch.setenv("K_SERVICE", "cubie-service")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)

    async def boom(request):
        raise RuntimeError("secret-password-and-database-host")

    response = asyncio.run(catch_exceptions_middleware(Mock(), boom))
    assert response.status_code == 500
    assert response.body == b'{"detail":"Internal Server Error"}'


def test_early_identity_refusal_keeps_cors_headers():
    from backend.main import app

    response = TestClient(app).get(
        "/api/users/me",
        headers={
            "Authorization": "Bearer opaque-google-token",
            "Origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_workspace_context_resets_after_request(monkeypatch):
    from rubik_core.auth import session_tokens
    from rubik_core.tenancy import middleware
    from rubik_core.tenancy.context import (
        NoWorkspaceContext,
        current_workspace,
        use_workspace,
    )

    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.setattr(
        middleware,
        "workspace_for_domain_any",
        lambda domain: SimpleNamespace(slug="acme", status="active"),
    )
    api = FastAPI()
    api.add_middleware(middleware.WorkspaceMiddleware)

    @api.get("/api/check")
    def check():
        return {"workspace": current_workspace()}

    @api.get("/probe")
    def probe():
        try:
            return {"workspace": current_workspace()}
        except NoWorkspaceContext:
            return {"workspace": None}

    with use_workspace("acme"):
        token = session_tokens.mint_session_token(
            "pat@acme.example", "acme", hd="acme.example", hours=1.0
        )[0]
    with TestClient(api) as client:
        response = client.get(
            "/api/check", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["workspace"] == "acme"
        assert client.get("/probe").json()["workspace"] is None


def test_control_lookup_does_not_block_other_async_requests(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from rubik_core.auth.session_tokens import mint_session_token
    from rubik_core.tenancy import middleware
    from rubik_core.tenancy.context import current_workspace, use_workspace

    rendezvous = Barrier(2)

    def lookup(domain):
        rendezvous.wait(timeout=3)
        return SimpleNamespace(slug="acme", status="active")

    monkeypatch.setattr(middleware, "workspace_for_domain_any", lookup)
    api = FastAPI()
    api.add_middleware(middleware.WorkspaceMiddleware)

    @api.get("/api/check")
    async def check():
        return {"workspace": current_workspace()}

    with use_workspace("acme"):
        token = mint_session_token(
            "pat@alias.example", "acme", hd="acme.example", hours=1.0
        )[0]

    async def concurrent_calls():
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://test"
        ) as client:
            return await asyncio.gather(
                client.get("/api/check", headers={"Authorization": f"Bearer {token}"}),
                client.get("/api/check", headers={"Authorization": f"Bearer {token}"}),
            )

    responses = asyncio.run(asyncio.wait_for(concurrent_calls(), timeout=5))
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["workspace"] for response in responses] == ["acme", "acme"]


def test_mock_email_domain_selects_one_workspace(monkeypatch):
    from rubik_core.tenancy import middleware
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", True)
    domains = {
        "acme.example": SimpleNamespace(slug="acme", status="active"),
        "other.example": SimpleNamespace(slug="other", status="active"),
    }
    monkeypatch.setattr(
        middleware, "workspace_for_domain_any", lambda domain: domains.get(domain)
    )
    api = FastAPI()
    api.add_middleware(middleware.WorkspaceMiddleware)

    @api.get("/api/check")
    def check():
        return {"workspace": current_workspace()}

    client = TestClient(api)
    for email, expected in (
        ("pat@acme.example", "acme"),
        ("jo@other.example", "other"),
    ):
        response = client.get("/api/check", headers={"X-Mock-User": email})
        assert response.status_code == 200
        assert response.json()["workspace"] == expected
    refused = client.get("/api/check", headers={"X-Mock-User": "jo@unknown.example"})
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "workspace_unknown"


def test_unknown_fathom_webhook_workspace_refused_before_tenant_db(monkeypatch):
    from backend.main import app
    from rubik_core.database import db

    monkeypatch.setattr(context, "_process_workspace", None)
    lookups = Mock(side_effect=AssertionError("tenant DB must not be touched"))
    monkeypatch.setattr(db, "engine_for", lookups)
    response = TestClient(app).post("/api/fathom/w/missing/webhook", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workspace_unknown"
    lookups.assert_not_called()


def test_production_session_secret_is_required_before_db(monkeypatch):
    from rubik_core.auth import session_tokens
    from rubik_core.tenancy import platform_secrets

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.delenv("CUBIE_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        platform_secrets.platform_secret("CUBIE_SESSION_SECRET")
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        session_tokens.decode_session_token("opaque")
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        session_tokens.mint_session_token("pat@acme.example", "test")

    monkeypatch.setenv("CUBIE_SESSION_SECRET", "weak")
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        session_tokens.decode_session_token("opaque")


def test_encoded_production_session_secret_signs_and_verifies(monkeypatch):
    from rubik_core.auth.session_tokens import decode_session_token, mint_session_token
    from rubik_core.tenancy.platform_secrets import platform_secret

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    key = (
        base64.urlsafe_b64encode(hashlib.sha256(b"identity-test-session-key").digest())
        .decode("ascii")
        .rstrip("=")
    )
    monkeypatch.setenv("CUBIE_SESSION_SECRET", key)
    assert platform_secret("CUBIE_SESSION_SECRET") == key
    token = mint_session_token("pat@acme.example", "test", hours=1.0)[0]
    assert decode_session_token(token).ws == "test"


def test_production_boot_refuses_weak_session_secret(monkeypatch):
    from backend.main import app

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setenv("CUBIE_SESSION_SECRET", "weak")
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        with TestClient(app):
            pytest.fail("GCP app started with a weak signing secret")


def test_cloud_run_never_enables_mock_auth_or_local_signing_key(monkeypatch):
    from backend.auth import get_current_user
    from backend.main import app
    from rubik_core.tenancy.platform_secrets import platform_secret

    monkeypatch.setenv("K_SERVICE", "cubie-service")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", True)
    monkeypatch.delenv("CUBIE_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        platform_secret("CUBIE_SESSION_SECRET")

    strong = (
        base64.urlsafe_b64encode(
            hashlib.sha256(b"identity-cloud-run-test-key").digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    monkeypatch.setenv("CUBIE_SESSION_SECRET", strong)
    with pytest.raises(RuntimeError, match="ENABLE_MOCK_AUTH"):
        with TestClient(app):
            pytest.fail("Cloud Run started with mock auth")
    request = Mock()
    request.headers = {"X-Mock-User": "attacker@acme.example"}
    credentials = Mock(credentials="mock-token")
    with pytest.raises(HTTPException) as refused:
        get_current_user(request, credentials)
    assert getattr(refused.value, "status_code", None) == 401
    response = TestClient(app).get(
        "/api/users/me", headers={"X-Mock-User": "attacker@acme.example"}
    )
    assert response.status_code == 401


def test_domain_lookup_uses_hd_and_reports_suspension(isolated_control_db):
    from rubik_core.control import repository
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace, WorkspaceDomain

    with control_session() as db:
        db.merge(Workspace(slug="acme", name="Acme", status="suspended"))
        db.merge(
            WorkspaceDomain(
                domain="acme.example", workspace_slug="acme", is_primary=True
            )
        )
        db.commit()
    row = repository.workspace_for_domain_any("ACME.EXAMPLE")
    assert row is not None and row.status == "suspended"
    assert repository.workspace_for_domain("acme.example") is None


def test_session_route_binds_verified_hd_not_email_suffix(monkeypatch):
    from backend.main import app
    from backend.routers import auth_session
    from rubik_core.auth.google_id_token import GoogleIdentity
    from rubik_core.auth.session_tokens import decode_session_token

    identity = GoogleIdentity(
        email="pat@alias.example",
        hd="acme.example",
        sub="g-1",
        name="Pat",
        picture=None,
    )
    monkeypatch.setattr(auth_session, "verify_google_id_token", lambda raw: identity)
    monkeypatch.setattr(
        auth_session,
        "workspace_for_domain_any",
        lambda hd: (
            SimpleNamespace(slug="test", status="active")
            if hd == "acme.example"
            else None
        ),
    )
    person = SimpleNamespace(email=identity.email)
    monkeypatch.setattr(
        auth_session, "_upsert_verified_user", lambda request, value: person
    )
    response = TestClient(app).post(
        "/api/auth/session", headers={"Authorization": "Bearer signed-id-token"}
    )
    assert response.status_code == 200, response.text
    claims = decode_session_token(response.json()["token"])
    assert claims.ws == "test"
    assert claims.hd == "acme.example"


@pytest.mark.parametrize(
    "row,code",
    [
        (None, "workspace_unknown"),
        (SimpleNamespace(slug="acme", status="suspended"), "workspace_suspended"),
    ],
)
def test_session_route_refuses_unknown_or_suspended_before_user_db(
    monkeypatch, row, code
):
    from backend.main import app
    from backend.routers import auth_session
    from rubik_core.auth.google_id_token import GoogleIdentity

    identity = GoogleIdentity(
        email="pat@acme.example", hd="acme.example", sub="g-1", name="Pat", picture=None
    )
    monkeypatch.setattr(auth_session, "verify_google_id_token", lambda raw: identity)
    monkeypatch.setattr(auth_session, "workspace_for_domain_any", lambda hd: row)
    upsert = Mock(side_effect=AssertionError("tenant DB must not be touched"))
    monkeypatch.setattr(auth_session, "_upsert_verified_user", upsert)
    response = TestClient(app).post(
        "/api/auth/session", headers={"Authorization": "Bearer signed-id-token"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == code
    upsert.assert_not_called()
