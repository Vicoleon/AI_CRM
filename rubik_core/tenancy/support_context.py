"""Trusted request-local identity for an audited platform support session."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SupportScope:
    actor: str
    sid: str
    workspace_slug: str
    workspace_name: str


_support: ContextVar[SupportScope | None] = ContextVar(
    "cubie_platform_support", default=None
)


def current_support_session() -> SupportScope | None:
    """Return only a support grant installed after live control validation."""
    return _support.get()


@contextmanager
def use_support_session(scope: SupportScope) -> Iterator[None]:
    token = _support.set(scope)
    try:
        yield
    finally:
        _support.reset(token)
