"""UC-40.2 — Lock serializes OAuth refresh across parallel Workspace clients.

The bug: when the priorities orchestrator runs 5 channel agents in parallel,
each agent independently calls ``credentials.refresh(Request())`` and the
concurrent refreshes race on the underlying SSL context, producing
``SSLEOFError`` on ``oauth2.googleapis.com``. A manual run hit this for
every user.

The fix: a module-level ``threading.Lock`` in
``rubik_core.tools._workspace_auth.safe_refresh`` so only one refresh
runs at a time per process. After that, API calls reuse cached tokens.

These tests verify the lock actually serializes, AND that all three
client ``_build_service`` methods route through it.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


def test_uc40_2_safe_refresh_serializes_concurrent_calls():
    """Five threads all hammering safe_refresh at once should see exactly one
    in the critical section at a time, never overlapping."""
    from rubik_core.tools._workspace_auth import safe_refresh

    overlap_observed = {"value": False}
    active_count = {"value": 0}
    active_count_lock = threading.Lock()

    def fake_refresh(_request):
        with active_count_lock:
            active_count["value"] += 1
            if active_count["value"] > 1:
                overlap_observed["value"] = True
        time.sleep(0.05)  # widen the window for any race to surface
        with active_count_lock:
            active_count["value"] -= 1

    creds = MagicMock()
    creds.refresh = fake_refresh

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(safe_refresh, creds) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # surface any exception

    assert overlap_observed["value"] is False, (
        "safe_refresh allowed concurrent refresh() calls — the lock didn't hold"
    )


def test_uc40_2_safe_refresh_calls_credentials_refresh():
    """safe_refresh must actually call creds.refresh(Request()) — the lock
    isn't a no-op."""
    from rubik_core.tools._workspace_auth import safe_refresh

    creds = MagicMock()
    safe_refresh(creds)
    creds.refresh.assert_called_once()
    # Argument should be a google.auth.transport.requests.Request instance —
    # checking the type name avoids importing the symbol if it's not on path.
    arg = creds.refresh.call_args.args[0]
    assert type(arg).__name__ == "Request"


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("gmail", "GmailClient"),
        ("calendar", "GoogleCalendarClient"),
        ("drive", "GoogleDriveClient"),
    ],
)
def test_uc40_2_clients_refresh_delegated_credentials_before_build(
    module_name, class_name
):
    """Every Google channel serializes refresh after selecting its user."""
    import importlib

    module = importlib.import_module(f"rubik_core.tools.{module_name}")
    fake_creds = MagicMock()
    fake_delegated = fake_creds.with_subject.return_value
    with (
        patch(
            "rubik_core.tools._workspace_auth.google_workspace_configured",
            return_value=True,
        ),
        patch(
            "rubik_core.tools._workspace_auth.load_workspace_credentials",
            return_value=fake_creds,
        ),
        patch("rubik_core.tools._workspace_auth.safe_refresh") as refresh,
        patch.object(module, "build") as build,
    ):
        getattr(module, class_name)(user_email="user@acme.example")
    fake_creds.with_subject.assert_called_once_with("user@acme.example")
    refresh.assert_called_once_with(fake_delegated)
    build.assert_called_once()
