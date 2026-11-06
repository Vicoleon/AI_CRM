"""Admin-only, server-side workspace AI credential and workload controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import admin_required
from backend.jobs.launcher import require_automations_http
from rubik_core.ai_providers.cloud_sources import CloudSourceError
from rubik_core.ai_providers.cloud_vector_index import CloudVectorIndexError
from rubik_core.ai_providers.config import ROUTABLE_FEATURES, public_catalog
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    configure_provider,
    model_routes,
    provider_status,
    revoke_provider,
    set_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, test_connection
from rubik_core.auth.registry import UserProfile
from rubik_core.config import AppMode, settings
from rubik_core.cost.usage import feature_context
from rubik_core.database.db import SessionLocal, get_db

router = APIRouter(prefix="/admin/workspace/ai", tags=["admin", "ai-providers"])


class ProviderConfigRequest(BaseModel):
    api_key: SecretStr | None = Field(default=None, repr=False)
    project: str | None = None
    region: str | None = None
    service_identity: str | None = None


class ModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)


class RouteRequest(ModelRequest):
    provider: str


class EmbeddingPrepareRequest(RouteRequest):
    corpus: str = "default"


class EmbeddingActivateRequest(BaseModel):
    version: str = Field(min_length=16, max_length=16)
    corpus: str = "default"


@router.get("")
def get_ai_settings(_: UserProfile = Depends(admin_required)) -> dict:
    return {
        "catalog": public_catalog(),
        "workloads": ROUTABLE_FEATURES,
        "providers": provider_status(),
        "routes": model_routes(),
    }


@router.put("/providers/{provider}")
def put_provider(
    provider: str,
    payload: ProviderConfigRequest,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict:
    before = provider_status().get(provider, {})
    try:
        configure_provider(
            provider,
            api_key=payload.api_key.get_secret_value() if payload.api_key else None,
            project=payload.project,
            region=payload.region,
            service_identity=payload.service_identity,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Provider setup failed") from None
    after = provider_status()[provider]
    record_audit(
        db,
        actor_email=user.email,
        action="WORKSPACE_AI_PROVIDER_SAVE",
        target=provider,
        details={
            "provider": provider,
            "was_connected": bool(before.get("connected")),
            "is_connected": bool(after.get("connected")),
            "credential_replaced": bool(before.get("credential_configured"))
            and provider != "vertex_client",
        },
    )
    return after


@router.delete("/providers/{provider}")
def delete_provider(
    provider: str,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict:
    before = provider_status().get(provider, {})
    try:
        revoke_provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="Provider revocation failed"
        ) from None
    after = provider_status()[provider]
    record_audit(
        db,
        actor_email=user.email,
        action="WORKSPACE_AI_PROVIDER_REVOKE",
        target=provider,
        details={"provider": provider, "was_connected": bool(before.get("connected"))},
    )
    return after


@router.post("/providers/{provider}/test")
def test_provider(
    provider: str,
    payload: ModelRequest,
    _: UserProfile = Depends(admin_required),
) -> dict:
    try:
        with feature_context("ai_provider_connection_test"):
            test_connection(provider, payload.model)
    except (ValueError, IncompatibleModelRoute) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=502, detail="Provider connection test failed"
        ) from None
    return {"status": "ok", "provider": provider, "model": payload.model}


@router.put("/routes/{feature}")
def put_route(
    feature: str,
    payload: RouteRequest,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict:
    # A route is activated only after the exact model/key combination works.
    before = model_routes().get(feature)
    try:
        capability = ROUTABLE_FEATURES.get(feature)
        if capability is None:
            raise ValueError("Workload is not available for provider routing yet")
        if feature == "embedding_index":
            raise IncompatibleModelRoute(
                "Prepare and activate an embedding index before changing its model"
            )
        if payload.provider != "vertex_managed":
            with feature_context("ai_provider_connection_test"):
                test_connection(payload.provider, payload.model, capability=capability)
        set_model_route(feature, payload.provider, payload.model)
    except (ValueError, IncompatibleModelRoute) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=502, detail="Provider connection test failed"
        ) from None
    record_audit(
        db,
        actor_email=user.email,
        action="WORKSPACE_AI_ROUTE_UPDATE",
        target=feature,
        details={
            "before": before,
            "after": {"provider": payload.provider, "model": payload.model},
        },
    )
    return {"feature": feature, "provider": payload.provider, "model": payload.model}


@router.get("/embedding-index")
def get_embedding_index(
    corpus: str = "default", _: UserProfile = Depends(admin_required)
) -> dict:
    from rubik_core.ai_providers.embedding_index import embedding_index_status

    try:
        return embedding_index_status(corpus=corpus)
    except (ValueError, IncompatibleModelRoute, CloudVectorIndexError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="Embedding index status is unavailable"
        ) from None


@router.post("/embedding-index/prepare")
def prepare_embedding_index_route(
    payload: EmbeddingPrepareRequest,
    user: UserProfile = Depends(admin_required),
) -> dict:
    require_automations_http()
    from rubik_core.ai_providers.embedding_index import prepare_embedding_index

    try:
        if settings.APP_MODE == AppMode.GCP:
            from backend.jobs.launcher import (
                launch_module_execution,
                reject_support_background_work,
            )
            from rubik_core.ai_providers.cloud_embedding_workflow import (
                fail_cloud_embedding_rebuild,
                queue_cloud_embedding_rebuild,
            )

            reject_support_background_work()
            result = queue_cloud_embedding_rebuild(
                payload.provider, payload.model, corpus=payload.corpus
            )
            try:
                launch_module_execution(
                    "backend.jobs.cloud_embedding_rebuild_job",
                    ["--run-id", result["run_id"]],
                )
            except Exception:
                fail_cloud_embedding_rebuild(result["corpus"], result["run_id"])
                raise HTTPException(
                    status_code=503,
                    detail="Embedding rebuild could not start. Please retry.",
                ) from None
        else:
            with feature_context("embedding_index_rebuild"):
                result = prepare_embedding_index(
                    payload.provider, payload.model, corpus=payload.corpus
                )
    except HTTPException:
        raise
    except (
        ValueError,
        IncompatibleModelRoute,
        CloudVectorIndexError,
        CloudSourceError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=502, detail="Embedding index rebuild failed"
        ) from None
    with SessionLocal() as db:
        record_audit(
            db,
            actor_email=user.email,
            action="WORKSPACE_EMBEDDING_INDEX_PREPARE",
            target=payload.corpus,
            details=result,
        )
    return result


@router.post("/embedding-index/activate")
def activate_embedding_index_route(
    payload: EmbeddingActivateRequest,
    user: UserProfile = Depends(admin_required),
) -> dict:
    from rubik_core.ai_providers.embedding_index import activate_embedding_index

    try:
        result = activate_embedding_index(payload.version, corpus=payload.corpus)
    except (
        ValueError,
        IncompatibleModelRoute,
        CloudVectorIndexError,
        CloudSourceError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="Embedding index activation failed"
        ) from None
    with SessionLocal() as db:
        record_audit(
            db,
            actor_email=user.email,
            action="WORKSPACE_EMBEDDING_INDEX_ACTIVATE",
            target=payload.corpus,
            details=result,
        )
    return result
