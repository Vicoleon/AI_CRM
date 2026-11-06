"""Credentials sent as HTTP header values can neither break a call nor leak.

A token stored with its trailing newline is refused by httpx as a header
value, quoted whole in its error, and an ``exc_info=True`` log line carries
that error into Cloud Logging. This pins the guarantees against that for
every credential Cubie sends in a header, HubSpot's first, since about twenty
call sites log HubSpot failures with ``exc_info=True``:

* workspace connector readers trim pasted token values before making headers;
* a HubSpot transport failure never puts the token in the error's message, or
  in the traceback a caller logs.

Every token here is fake.
"""

from __future__ import annotations

import logging

import h11
import httpx
import pytest
import requests
from sqlalchemy import delete

from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.integrations.fathom import FathomClient
from rubik_core.logsafe import safe_error
from rubik_core.people import config as people_config
from rubik_core.retrieval.hubspot_api import HubSpotAPIClient, HubSpotAPIError
from rubik_core.retrieval.hubspot_write import HubSpotWriteClient, HubSpotWriteError
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.secrets import bind_workspace_secret_ref, write_secret

TOKEN = "pat-na1-SECRETVALUE"  # shaped like a HubSpot private-app token


def _h11_refusal(header: str, value: str) -> h11.LocalProtocolError:
    """The error h11, under httpx, raises for an illegal header value."""
    try:
        h11.Request(
            method="POST",
            target="/",
            headers=[("Host", "api.hubapi.com"), (header, value)],
        )
    except h11.LocalProtocolError as exc:
        return exc
    raise AssertionError(f"h11 accepted {header} as a header value")


def _httpx_refusal() -> httpx.LocalProtocolError:
    """What httpx raises for a token saved with its newline: h11's message,
    with h11's error chained underneath, as httpx's own mapping leaves it."""
    cause = _h11_refusal("Authorization", f"Bearer {TOKEN}\n")
    refused = httpx.LocalProtocolError(str(cause))
    refused.__cause__ = cause
    return refused


def _client_raising(make_exc):
    """Stands in for httpx.Client: every request fails with ``make_exc()``."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def _fail(self, *args, **kwargs):
            raise make_exc()

        get = post = request = _fail

    return _Client


def _assert_never_logged(exc: BaseException, caplog) -> None:
    """Neither the message nor the traceback a caller logs carries the token."""
    assert "SECRETVALUE" not in str(exc)
    # The leak path: a caller logging the failure with exc_info=True.
    with caplog.at_level(logging.WARNING):
        logging.getLogger("cockpit").warning("HubSpot read failed", exc_info=exc)
    assert "HubSpot read failed" in caplog.text
    assert "SECRETVALUE" not in caplog.text


# ── workspace connector readers normalize header values ──────────────────────


@pytest.fixture
def header_token_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    with control_session() as db:
        db.add(Workspace(slug="header-tokens", name="Header Tokens", status="active"))
        db.commit()
    try:
        with use_workspace("header-tokens"):
            for connector, field in (
                ("hubspot", "read_token"),
                ("hubspot", "write_token"),
                ("fathom", "api_key"),
                ("linkedin", "data_api_key"),
            ):
                ref = bind_workspace_secret_ref(connector, field)
                write_secret(ref, f" {TOKEN}\r\n")
            yield
    finally:
        with control_session() as db:
            db.execute(
                delete(WorkspaceConnector).where(
                    WorkspaceConnector.workspace_slug == "header-tokens"
                )
            )
            row = db.get(Workspace, "header-tokens")
            if row is not None:
                db.delete(row)
            db.commit()


def test_workspace_header_tokens_are_trimmed_before_use(header_token_workspace):
    read_headers = HubSpotAPIClient()._headers()
    write_headers = HubSpotWriteClient()._headers()
    fathom_headers = FathomClient().headers
    linkedin_key = people_config.linkedin_api_key()

    assert read_headers["Authorization"] == f"Bearer {TOKEN}"
    assert write_headers["Authorization"] == f"Bearer {TOKEN}"
    assert fathom_headers["Authorization"] == f"Bearer {TOKEN}"
    assert fathom_headers["X-Api-Key"] == TOKEN
    assert linkedin_key == TOKEN
    for headers in (read_headers, write_headers, fathom_headers):
        h11.Request(
            method="POST",
            target="/",
            headers=[("Host", "api.example.com"), *headers.items()],
        )


# ── the masker ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "header,value",
    [
        ("Authorization", f"Bearer {TOKEN}\n"),
        ("X-Api-Key", "fathom-SECRETVALUE\n"),
        ("X-DD-Token", "dd-SECRETVALUE\n"),
    ],
)
def test_safe_error_masks_a_refused_header_whatever_its_name(header, value):
    text = safe_error(httpx.LocalProtocolError(str(_h11_refusal(header, value))))
    assert text.startswith("LocalProtocolError: Illegal header value")
    assert "SECRETVALUE" not in text and "[redacted]" in text


def test_safe_error_masks_the_requests_wording_too():
    """Fathom calls through requests, which words the same refusal its own way."""
    with pytest.raises(requests.exceptions.InvalidHeader) as raised:
        requests.Request(
            "GET",
            "https://api.fathom.ai/external/v1/meetings",
            headers={"X-Api-Key": "fathom-SECRETVALUE\n"},
        ).prepare()
    text = safe_error(raised.value)
    assert "SECRETVALUE" not in text and "[redacted]" in text


def test_safe_error_masks_a_bare_hubspot_token():
    assert TOKEN not in safe_error(RuntimeError(f"HubSpot echoed {TOKEN} back"))


# ── a HubSpot transport failure keeps the token out of the logs ──────────────


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda c: c.count("deals", []), id="search"),
        pytest.param(
            lambda c: c.batch_read("deals", ["1"], ["dealname"]), id="batch-read"
        ),
        pytest.param(
            lambda c: c.batch_associations("deals", "companies", ["1"]),
            id="associations",
        ),
    ],
)
def test_a_refused_hubspot_read_never_logs_the_token(monkeypatch, caplog, read):
    monkeypatch.setattr("httpx.Client", _client_raising(_httpx_refusal))
    with pytest.raises(HubSpotAPIError) as raised:
        read(HubSpotAPIClient(token="tok"))
    _assert_never_logged(raised.value, caplog)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda w: w._request("PATCH", "/crm/v3/objects/deals/1", {}), id="write"
        ),
        pytest.param(
            lambda w: w.get_object("deals", "1", ["dealname"]), id="gate-read"
        ),
    ],
)
def test_a_refused_hubspot_write_never_logs_the_token(monkeypatch, caplog, call):
    monkeypatch.setattr("httpx.Client", _client_raising(_httpx_refusal))
    writer = HubSpotWriteClient(token="tok", read_client=HubSpotAPIClient(token="tok"))
    with pytest.raises(HubSpotWriteError) as raised:
        call(writer)
    _assert_never_logged(raised.value, caplog)


def test_a_transport_failure_still_says_what_went_wrong(monkeypatch):
    from types import SimpleNamespace

    def timeout():
        return httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.get_hubspot_config",
        lambda: SimpleNamespace(pipeline_ids=("pipeline-1",)),
    )
    monkeypatch.setattr("httpx.Client", _client_raising(timeout))
    with pytest.raises(HubSpotAPIError) as raised:
        HubSpotAPIClient(token="tok").count("deals", [])
    assert str(raised.value) == "HubSpot transport error: ConnectTimeout: timed out"
