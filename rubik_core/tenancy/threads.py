"""Carry request and workspace context across explicit Python thread hops."""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

_Result = TypeVar("_Result")


def submit_with_context(
    executor: ThreadPoolExecutor, fn: Callable[..., _Result], *args: Any, **kwargs: Any
) -> Future[_Result]:
    """Submit with the caller's contextvars, including its workspace scope."""
    context = contextvars.copy_context()
    return executor.submit(context.run, fn, *args, **kwargs)


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """A pool whose every submission inherits the submitting caller's context."""

    def submit(  # type: ignore[override]
        self, fn: Callable[..., _Result], *args: Any, **kwargs: Any
    ) -> Future[_Result]:
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)


def context_thread(
    fn: Callable[..., Any],
    *args: Any,
    name: str | None = None,
    daemon: bool | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> threading.Thread:
    """Build a thread with a snapshot of the current context; caller starts it."""
    context = contextvars.copy_context()
    return threading.Thread(
        target=context.run,
        args=(fn, *args),
        kwargs=dict(kwargs or {}),
        name=name,
        daemon=daemon,
    )
