"""Explicit request scopes and opt-in defaults for non-server processes."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class NoWorkspaceContext(RuntimeError):
    """The caller has not selected an authorized workspace."""


_workspace: ContextVar[str | None] = ContextVar("cubie_workspace", default=None)
_process_workspace: str | None = None


def current_workspace() -> str:
    """Return the explicit scope, or an authorized Job/CLI process default.

    Merely setting CUBIE_WORKSPACE does not authorize a default. Entry points
    must first call bootstrap_process_workspace(). Server processes never use
    a process default, including one installed before K_SERVICE was set.
    """
    slug = _workspace.get()
    if slug is not None:
        return slug
    if _process_workspace is not None and "K_SERVICE" not in os.environ:
        return _process_workspace
    raise NoWorkspaceContext("Select a workspace before accessing workspace resources")


def _canonical_slug(slug: str) -> str:
    """Reject ambiguous workspace keys before they enter context or file paths."""
    if (
        not isinstance(slug, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        or len(slug) > 63
    ):
        raise ValueError("A normalized workspace slug is required")
    return slug


@contextmanager
def use_workspace(slug: str) -> Iterator[None]:
    """Bind an already-resolved workspace for this execution context.

    Request authentication/resolution validates the control row before binding.
    The token restores nested callers even when the enclosed operation fails.
    """
    _canonical_slug(slug)
    token = _workspace.set(slug)
    try:
        yield
    finally:
        _workspace.reset(token)


def _validate_workspace(slug: str) -> None:
    # Lazy to keep importing request context independent of control DB startup.
    from rubik_core.control.repository import get_workspace

    if slug == "test" and "pytest" in sys.modules:
        return
    workspace = get_workspace(slug)
    if workspace is None or workspace.status != "active":
        raise ValueError(f"Workspace {slug!r} is unknown or inactive")


def bootstrap_process_workspace(slug: str | None = None) -> str:
    """Validate and install the explicit default for a Job, CLI, or pytest.

    Cloud Run services must bind each request independently and cannot opt in.
    A failed validation leaves any existing authorized default untouched.
    """
    if "K_SERVICE" in os.environ:
        raise NoWorkspaceContext("K_SERVICE servers cannot install a process workspace")
    selected = slug if slug is not None else os.environ.get("CUBIE_WORKSPACE")
    if not isinstance(selected, str) or not selected.strip():
        raise NoWorkspaceContext("Pass a workspace slug or set CUBIE_WORKSPACE")
    selected = _canonical_slug(selected.strip().lower())
    _validate_workspace(selected)
    global _process_workspace
    _process_workspace = selected
    return selected
