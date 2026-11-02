"""Pure ASGI workspace selection before FastAPI dependencies can open a DB."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
from tempfile import SpooledTemporaryFile
from urllib.parse import parse_qs

import anyio
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from rubik_core.auth.session_tokens import SessionExpiredError, decode_session_token
from rubik_core.config import AppMode, settings
from rubik_core.control.repository import (
    get_workspace,
    validate_support_session,
    workspace_for_domain_any,
)
from rubik_core.tenancy import context
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.scheduler import run_for_workspaces, scheduler_scope
from rubik_core.tenancy.support_context import use_support_session

logger = logging.getLogger(__name__)
_FATHOM_WEBHOOK_PATH = re.compile(
    r"\A/api/fathom/w/([a-z0-9]+(?:-[a-z0-9]+)*)/webhook\Z"
)


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"detail": {"code": code, "message": message}}
    )


def _is_unresolved_inbound(path: str) -> bool:
    return path in (
        "/api/slack/commands",
        "/api/slack/interactivity",
        "/api/slack/callback",
        "/api/email/accounts/callback",
    )


_SCHEDULER_PATHS = frozenset(
    {
        "/api/agents/ingest/eos",
        "/api/agents/eos/l10/summary-emails",
        "/api/agents/eos/l10/close-abandoned-sessions",
        "/api/agents/availability/refresh-ooo",
        "/api/agents/overnight/run",
        "/api/agents/priorities/deliver-slack",
        "/api/agents/priorities/manager-digest",
        "/api/agents/scorecard/sync-sales",
        "/api/agents/fathom/sync",
        "/api/agents/jobs/ccs/sweep",
        "/api/agents/jobs/cockpit/recompute-states",
        "/api/agents/jobs/cockpit/stage-lag",
        "/api/agents/jobs/cockpit/sow-sent",
        "/api/agents/jobs/cockpit/daily-digest",
        "/api/agents/jobs/cockpit/weekly-aging",
        "/api/agents/jobs/loop-closure/scan",
        "/api/agents/scheduled-tasks/tick",
        "/api/agents/whats-new/digest",
        "/api/jobs/overnight-briefing",
        "/api/jobs/priorities/run-all",
        "/api/jobs/priorities/run-next-week",
        "/api/jobs/ingestion-council/run",
        "/api/ingest/scheduler/sync",
        "/api/admin/transcript-sync",
        "/api/admin/transcript-acl-backfill",
    }
)
_NAMED_SCHEDULER_ONLY_PATHS = frozenset({"/api/jobs/overnight-briefing"})


def _scheduler_module(path: str, query_string: bytes) -> str | None:
    return "eos" if _is_eos_path(path, query_string) else None


def _is_eos_path(path: str, query_string: bytes = b"") -> bool:
    """EOS-only paths whose module switch must run before any tenant DB dep."""
    if path == "/api/eos" or path.startswith("/api/eos/"):
        return True
    if path in (
        "/api/agents/ingest/eos",
        "/api/agents/eos/l10/summary-emails",
        "/api/agents/eos/l10/close-abandoned-sessions",
        "/api/agents/scorecard/sync-sales",
        "/api/sales/loose-ends/team/scorecard-setup",
    ):
        return True
    if path.startswith("/api/users/") and path.endswith("/eos-teams"):
        return True
    if path in (
        "/api/jobs/ingestion-council/run",
        "/api/agents/jobs/ingestion-council/run",
    ):
        return any(
            scope in ("weekly", "all")
            for scope in parse_qs(query_string.decode("latin-1")).get("scope", [])
        )
    return path.startswith("/api/todos/") and path.endswith("/promote-eos")


def _test_workspace() -> bool:
    # Existing FastAPI tests override auth and omit credentials. This explicit,
    # pytest-only process bootstrap preserves them without creating a server
    # default or excusing a supplied invalid credential.
    return (
        "pytest" in sys.modules
        and settings.APP_MODE != AppMode.GCP
        and not os.environ.get("K_SERVICE")
        and context._process_workspace == "test"
    )


class WorkspaceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if (
            scope.get("method") == "OPTIONS"
            or not path.startswith("/api/")
            or path == "/api/auth/session"
        ):
            return await self.app(scope, receive, send)

        if path == "/api/fathom/webhook":
            return await _error(404, "webhook_moved", "Webhook not found")(
                scope, receive, send
            )

        # The URL selects the Fathom installation. Do this before FastAPI
        # dependencies can open a tenant session, and ignore bearer/mock auth.
        # The workspace-specific webhook secret is then verified in the router.
        if path.startswith("/api/fathom/w/"):
            match = _FATHOM_WEBHOOK_PATH.fullmatch(path)
            if match is None or len(match.group(1)) > 63:
                return await _error(404, "workspace_unknown", "Webhook not found")(
                    scope, receive, send
                )
            slug = match.group(1)
            try:
                workspace = await run_in_threadpool(get_workspace, slug)
            except Exception:
                logger.exception("Fathom workspace lookup failed")
                return await _error(
                    503, "workspace_unavailable", "Webhook unavailable"
                )(scope, receive, send)
            if workspace is None or workspace.status != "active":
                return await _error(404, "workspace_unknown", "Webhook not found")(
                    scope, receive, send
                )
            scope.setdefault("state", {})["workspace"] = slug
            with use_workspace(slug):
                return await self._selected_app(scope, receive, send)

        # Slack commands/interactivity verify the selected installation's raw
        # body signature; the OAuth callback verifies a platform-signed state.
        # All three async handlers bind the verified workspace before tenant
        # DB work. A bearer/mock session must never select their tenant.
        method = scope.get("method")
        if (
            method == "POST"
            and path in ("/api/slack/commands", "/api/slack/interactivity")
        ) or (
            method == "GET"
            and path in ("/api/slack/callback", "/api/email/accounts/callback")
        ):
            return await self.app(scope, receive, send)

        headers = _headers(scope)
        if "x-scheduler-key" in headers:
            if scope.get("method") != "POST" or path not in _SCHEDULER_PATHS:
                return await _error(
                    403, "scheduler_path_denied", "Scheduler path denied"
                )(scope, receive, send)
            try:
                selected = await run_in_threadpool(
                    scheduler_scope,
                    Request(scope),
                    _scheduler_module(path, scope.get("query_string", b"")),
                )
            except HTTPException as exc:
                return await _error(
                    exc.status_code, "scheduler_scope_denied", str(exc.detail)
                )(scope, receive, send)
            except Exception:
                logger.exception("Scheduler control lookup failed")
                return await _error(
                    503, "workspace_unavailable", "Workspace routing unavailable"
                )(scope, receive, send)
            if selected.fanout:
                if path in _NAMED_SCHEDULER_ONLY_PATHS:
                    return await _error(
                        400,
                        "workspace_required",
                        "X-Cubie-Workspace is required for this target",
                    )(scope, receive, send)
                return await self._fanout_scheduler(
                    scope, receive, send, selected.slugs
                )
            slug = selected.slugs[0]
            scope.setdefault("state", {})["workspace"] = slug
            with use_workspace(slug):
                return await self._selected_app(scope, receive, send)

        authorization = headers.get("authorization", "")
        claims = None
        support = None
        if authorization:
            prefix, _, raw = authorization.partition(" ")
            if prefix.lower() != "bearer" or not raw or " " in raw:
                return await _error(401, "invalid_session", "Sign in again")(
                    scope, receive, send
                )
            if (
                settings.ENABLE_MOCK_AUTH
                and settings.APP_MODE != AppMode.GCP
                and not os.environ.get("K_SERVICE")
                and (raw == "mock-token" or "@" in raw)
            ):
                if _test_workspace():
                    slug = "test"
                else:
                    mock_email = headers.get("x-mock-user") or (
                        raw if "@" in raw else settings.CUBIE_DEV_USER_EMAIL
                    )
                    result = await _workspace_from_mock_email(mock_email)
                    if isinstance(result, JSONResponse):
                        return await result(scope, receive, send)
                    slug = result
            elif raw.startswith(("atlx_", "cbx_")):
                if not path.startswith("/api/browser-extension/"):
                    return await _error(401, "invalid_session", "Sign in again")(
                        scope, receive, send
                    )
                # Verify the signed routing slug before consulting control data
                # or opening a tenant database. Legacy atlx_ keys cannot route.
                from rubik_core.browser_extension.connections import (
                    parse_extension_workspace,
                )

                try:
                    slug = parse_extension_workspace(raw)
                except RuntimeError:
                    logger.exception("Extension route signing secret unavailable")
                    return await _error(
                        503, "workspace_unavailable", "Workspace is unavailable"
                    )(scope, receive, send)
                if slug is None:
                    return await _error(401, "invalid_session", "Connect again")(
                        scope, receive, send
                    )
                try:
                    workspace = await run_in_threadpool(get_workspace, slug)
                except Exception:
                    logger.exception("Extension workspace lookup failed")
                    return await _error(
                        503, "workspace_unavailable", "Workspace is unavailable"
                    )(scope, receive, send)
                if workspace is None or workspace.status != "active":
                    return await _error(401, "workspace_unavailable", "Connect again")(
                        scope, receive, send
                    )
            else:
                try:
                    claims = decode_session_token(raw)
                except SessionExpiredError:
                    return await _error(401, "session_expired", "Sign in again")(
                        scope, receive, send
                    )
                if claims is None:
                    return await _error(401, "invalid_session", "Sign in again")(
                        scope, receive, send
                    )
                slug = claims.ws
                if claims.typ == "support":
                    if claims.sub != claims.sup:
                        return await _error(401, "invalid_session", "Sign in again")(
                            scope, receive, send
                        )
                    try:
                        support = await run_in_threadpool(
                            validate_support_session, claims.sid, claims.sup, slug
                        )
                    except Exception:
                        logger.exception("Platform support control validation failed")
                        return await _error(
                            503, "support_unavailable", "Support access is unavailable"
                        )(scope, receive, send)
                    if support is None:
                        return await _error(
                            401,
                            "support_revoked",
                            "Support session is no longer active",
                        )(scope, receive, send)
                elif claims.typ != "user":
                    return await _error(401, "invalid_session", "Sign in again")(
                        scope, receive, send
                    )
                elif not (slug == "test" and "pytest" in sys.modules):
                    workspace = await run_in_threadpool(
                        workspace_for_domain_any, claims.hd
                    )
                    if (
                        workspace is None
                        or workspace.status != "active"
                        or workspace.slug != slug
                    ):
                        return await _error(
                            401, "workspace_unavailable", "Workspace is unavailable"
                        )(scope, receive, send)
        elif _test_workspace():
            slug = "test"
        elif (
            settings.ENABLE_MOCK_AUTH
            and settings.APP_MODE != AppMode.GCP
            and not os.environ.get("K_SERVICE")
        ):
            mock_email = headers.get("x-mock-user") or settings.CUBIE_DEV_USER_EMAIL
            result = await _workspace_from_mock_email(mock_email)
            if isinstance(result, JSONResponse):
                return await result(scope, receive, send)
            slug = result
        elif _is_unresolved_inbound(path):
            return await _error(
                503, "workspace_not_resolved", "Workspace routing is not ready"
            )(scope, receive, send)
        else:
            return await _error(401, "missing_session", "Sign in to continue")(
                scope, receive, send
            )

        requested = headers.get("x-cubie-workspace")
        if requested is not None and requested != slug:
            return await _error(403, "workspace_mismatch", "Workspace mismatch")(
                scope, receive, send
            )

        scope.setdefault("state", {})["workspace"] = slug
        if claims is not None:
            scope["state"]["session_claims"] = claims
        if support is not None:
            scope["state"]["platform_support"] = support
        with use_workspace(slug):
            if support is not None:
                with use_support_session(support):
                    if scope.get("method", "GET").upper() not in (
                        "GET",
                        "HEAD",
                        "OPTIONS",
                    ):
                        return await self._audited_support_request(scope, receive, send)
                    return await self._selected_app(scope, receive, send)
            return await self._selected_app(scope, receive, send)

    async def _fanout_scheduler(self, scope, receive, send, slugs: tuple[str, ...]):
        """Replay one bounded scheduler request under each verified scope."""
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return await _error(400, "invalid_request", "Invalid request body")(
                    scope, receive, send
                )
            body.extend(message.get("body", b""))
            if len(body) > 1024 * 1024:
                return await _error(
                    413, "scheduler_body_too_large", "Request too large"
                )(scope, receive, send)
            if not message.get("more_body", False):
                break

        async def invoke(slug: str):
            child_scope = dict(scope)
            child_scope["state"] = {**scope.get("state", {}), "workspace": slug}
            child_scope["headers"] = list(scope.get("headers", [])) + [
                (b"x-cubie-workspace", slug.encode("ascii"))
            ]
            started = None
            chunks: list[bytes] = []
            response_size = 0
            delivered = False

            async def child_receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            async def child_send(message):
                nonlocal started, response_size
                if message["type"] == "http.response.start":
                    started = int(message["status"])
                elif message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    response_size += len(chunk)
                    if response_size > 1024 * 1024:
                        raise RuntimeError("Scheduler workspace result too large")
                    chunks.append(chunk)

            await self._selected_app(child_scope, child_receive, child_send)
            payload = b"".join(chunks)
            try:
                result = json.loads(payload) if payload else None
            except (ValueError, UnicodeDecodeError):
                result = payload.decode("utf-8", errors="replace")[:1000]
            if started is None or started >= 400:
                raise RuntimeError(f"HTTP {started or 500}")
            return result

        result = await run_for_workspaces(slugs, invoke, limit=4)
        for slug, outcome in result.items():
            logger.info("Scheduler workspace %s: %s", slug, outcome["status"])
        status_code = (
            502
            if any(outcome["status"] == "error" for outcome in result.values())
            else 200
        )
        return await JSONResponse({"workspaces": result}, status_code=status_code)(
            scope, receive, send
        )

    async def _selected_app(self, scope, receive, send):
        """Apply the EOS switch before any tenant route dependency runs."""
        path = scope.get("path", "")
        if _is_eos_path(path, scope.get("query_string", b"")):
            from rubik_core.control.repository import module_enabled

            if not await run_in_threadpool(
                module_enabled, "eos", scope["state"]["workspace"]
            ):
                return await _error(404, "module_off", "EOS is not enabled")(
                    scope, receive, send
                )
        return await self.app(scope, receive, send)

    async def _audited_support_request(self, scope, receive, send):
        """Commit support intent before route work, outcome before response."""
        from backend.audit import begin_support_audit, finish_support_audit

        support = scope["state"]["platform_support"]
        try:
            audit_id = await run_in_threadpool(
                begin_support_audit,
                actor=support.actor,
                sid=support.sid,
                method=scope["method"].upper(),
                path=scope.get("path", ""),
                request_id=secrets.token_hex(8),
            )
        except Exception:
            logger.exception("Platform support intent could not be committed")
            return await _error(
                503, "support_audit_unavailable", "Support audit is unavailable"
            )(scope, receive, send)

        start = None
        status_code = None
        stream = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")

        async def capture(message):
            nonlocal start, status_code
            if message["type"] == "http.response.start":
                start = message
                status_code = int(message["status"])
            elif message["type"] == "http.response.body":
                stream.write(message.get("body", b""))
            else:
                raise RuntimeError("Unsupported platform support response event")

        error = None
        try:
            try:
                await self._selected_app(scope, receive, capture)
            except BaseException as exc:
                error = exc
            outcome = (
                "success"
                if error is None and status_code is not None and status_code < 400
                else "failure"
            )
            # Shield the durable finish even when the client disconnects. A
            # failed finish leaves the precommitted pending row for operations.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(
                    finish_support_audit,
                    audit_id,
                    outcome=outcome,
                    status_code=status_code,
                )
            if error is not None:
                raise error
            if start is None:
                raise RuntimeError("Support route returned no HTTP response")
            await send(start)
            stream.seek(0)
            chunk = stream.read(64 * 1024)
            while chunk:
                next_chunk = stream.read(64 * 1024)
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": bool(next_chunk),
                    }
                )
                chunk = next_chunk
            if stream.tell() == 0:
                await send({"type": "http.response.body", "body": b""})
        finally:
            stream.close()


async def _workspace_from_mock_email(email: str) -> str | JSONResponse:
    if not isinstance(email, str) or email.count("@") != 1:
        return _error(403, "workspace_personal", "Use a workspace email")
    domain = email.rsplit("@", 1)[1].strip().lower()
    workspace = await run_in_threadpool(workspace_for_domain_any, domain)
    if workspace is None:
        return _error(403, "workspace_unknown", "Your company is not set up")
    if workspace.status != "active":
        return _error(403, "workspace_suspended", "Workspace is unavailable")
    return workspace.slug
