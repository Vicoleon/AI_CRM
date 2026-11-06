"""Workspace binding is explicit and restores its caller's scope."""

import asyncio

import pytest

from rubik_core.tenancy import context


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CUBIE_WORKSPACE", raising=False)
    token = context._workspace.set(None)
    yield
    context._workspace.reset(token)


def test_unbound_server_has_no_workspace(monkeypatch):
    monkeypatch.setenv("CUBIE_WORKSPACE", "acme")
    with pytest.raises(context.NoWorkspaceContext):
        context.current_workspace()


def test_nested_binding_restores_after_exception():
    with context.use_workspace("acme"):
        with pytest.raises(RuntimeError), context.use_workspace("other"):
            assert context.current_workspace() == "other"
            raise RuntimeError("failed request")
        assert context.current_workspace() == "acme"
    with pytest.raises(context.NoWorkspaceContext):
        context.current_workspace()


def test_async_tasks_have_independent_bindings():
    async def worker(slug):
        with context.use_workspace(slug):
            await asyncio.sleep(0)
            return context.current_workspace()

    async def run():
        return await asyncio.gather(worker("acme"), worker("other"))

    assert asyncio.run(run()) == ["acme", "other"]


def test_bootstrap_requires_explicit_selection():
    with pytest.raises(context.NoWorkspaceContext):
        context.bootstrap_process_workspace()


def test_bootstrap_validates_env_and_explicit_wins(monkeypatch):
    validated = []
    monkeypatch.setattr(context, "_validate_workspace", validated.append)
    monkeypatch.setenv("CUBIE_WORKSPACE", "acme")
    assert context.bootstrap_process_workspace() == "acme"
    assert context.current_workspace() == "acme"
    assert context.bootstrap_process_workspace("other") == "other"
    assert validated == ["acme", "other"]
    with context.use_workspace("request"):
        assert context.current_workspace() == "request"
    assert context.current_workspace() == "other"


def test_server_refuses_process_default_even_with_explicit_slug(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "cubie")
    monkeypatch.setattr(context, "_validate_workspace", lambda slug: None)
    with pytest.raises(context.NoWorkspaceContext, match="K_SERVICE"):
        context.bootstrap_process_workspace("acme")
    with pytest.raises(context.NoWorkspaceContext):
        context.current_workspace()


@pytest.mark.parametrize("slug", ["unknown", "suspended"])
def test_failed_validation_does_not_install_default(monkeypatch, slug):
    def reject(value):
        raise ValueError(f"Workspace {value} is unavailable")

    monkeypatch.setattr(context, "_validate_workspace", reject)
    with pytest.raises(ValueError, match=slug):
        context.bootstrap_process_workspace(slug)
    with pytest.raises(context.NoWorkspaceContext):
        context.current_workspace()


@pytest.mark.parametrize("status", ["active", "suspended", "provisioning", None])
def test_bootstrap_validates_real_control_rows(status):
    from rubik_core.control.db import control_session, get_engine
    from rubik_core.control.models import Base, Workspace

    Base.metadata.create_all(get_engine())
    with control_session() as session:
        if status is not None:
            session.add(
                Workspace(slug="bootstrap-check", name="Bootstrap", status=status)
            )
            session.commit()
    try:
        if status == "active":
            assert (
                context.bootstrap_process_workspace("bootstrap-check")
                == "bootstrap-check"
            )
        else:
            with pytest.raises(ValueError, match="unknown or inactive"):
                context.bootstrap_process_workspace("bootstrap-check")
            with pytest.raises(context.NoWorkspaceContext):
                context.current_workspace()
    finally:
        with control_session() as session:
            row = session.get(Workspace, "bootstrap-check")
            if row is not None:
                session.delete(row)
                session.commit()


def test_bootstrap_uses_canonical_control_slug(monkeypatch):
    validated = []
    monkeypatch.setattr(context, "_validate_workspace", validated.append)
    assert context.bootstrap_process_workspace(" Acme ") == "acme"
    assert context.current_workspace() == "acme"
    assert validated == ["acme"]


@pytest.mark.parametrize(
    "slug", ["../escape", "Acme", " acme", "acme ", "a/b", "a_b", "", "a" * 64]
)
def test_context_rejects_noncanonical_slugs(slug):
    with pytest.raises(ValueError):
        with context.use_workspace(slug):
            pytest.fail("Invalid workspace was bound")


@pytest.mark.parametrize("slug", ["../escape", "a/b", "a_b", "a" * 64])
def test_bootstrap_rejects_pathlike_and_invalid_slugs(monkeypatch, slug):
    validator = []
    monkeypatch.setattr(context, "_validate_workspace", validator.append)
    with pytest.raises(ValueError):
        context.bootstrap_process_workspace(slug)
    assert validator == []
