"""REST CRUD for API keys (SPECS §13.7-13.10).

Pre-Sprint 13 the only way to manage keys programmatically was by
driving the Jinja form; clustering / automation / external SDKs had no
affordance.  This router exposes the same flows over JSON and shares
:class:`~app.auth.key_service.ApiKeyService` with the UI so the two
surfaces cannot drift.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import require_admin_key
from app.auth.key_service import ApiKeyService
from app.dependencies import container


def _service() -> ApiKeyService:
    # Resolved per request so the atomic Container.reload() swap is
    # picked up by long-lived connections.
    return ApiKeyService(container.api_keys, container.store)


router = APIRouter(
    prefix="/admin/v1/keys",
    tags=["admin", "keys"],
    dependencies=[Depends(require_admin_key)],
)


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    # Reject typos up-front so a PATCH with ``actoin`` isn't silently a no-op.
    model_config = ConfigDict(extra="forbid")


class CreateKeyRequest(_StrictModel):
    key_id: str = Field(..., min_length=1, max_length=64)


class PatchKeyRequest(_StrictModel):
    action: Literal["activate", "suspend", "revoke", "rotate"]


class KeySummary(_StrictModel):
    """Public record: never includes the raw secret or the stored hash."""

    key_id: str
    status: str
    prefix: str
    created_at: str
    last_used_at: str | None = None


class KeyCreateResponse(_StrictModel):
    """Returned once on creation; the secret is never persisted in plaintext."""

    key_id: str
    key: str
    created_at: str
    prefix: str


class KeyListResponse(_StrictModel):
    keys: list[KeySummary]


class KeyRotateResponse(_StrictModel):
    key_id: str
    key: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=KeyListResponse)
def list_keys() -> KeyListResponse:
    """Return every known key without disclosing its secret or hash."""
    svc = _service()
    return KeyListResponse(keys=[KeySummary(**asdict(k)) for k in svc.list_keys()])


@router.get("/{key_id}", response_model=KeySummary)
def get_key(key_id: str) -> KeySummary:
    """Return a single key record by its public label."""
    svc = _service()
    return KeySummary(**asdict(svc.get_key(key_id)))


@router.post("", response_model=KeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_key(payload: CreateKeyRequest) -> KeyCreateResponse:
    """Create a new API key.

    The returned ``key`` value is the only time the raw secret is
    disclosed; callers are expected to store it themselves.  EGG-API
    keeps only a hash.
    """
    svc = _service()
    created = svc.create(payload.key_id)
    return KeyCreateResponse(
        key_id=created.key_id,
        key=created.key,
        created_at=created.created_at,
        # The store returns the prefix as part of ApiKey.key[:8]; recompute
        # here so clients can match on list_keys().prefix without extra calls.
        prefix=created.key[:8],
    )


@router.patch("/{key_id}")
def patch_key(key_id: str, payload: PatchKeyRequest) -> dict[str, object]:
    """Transition a key: activate / suspend / revoke / rotate.

    ``suspend`` and ``revoke`` immediately invalidate every admin UI
    session bound to this ``key_id``; ``rotate`` additionally returns
    the new raw secret (the only time it is ever disclosed).
    """
    svc = _service()
    if payload.action == "rotate":
        rotated = svc.rotate(key_id)
        return {"key_id": rotated.key_id, "key": rotated.key, "status": "active"}
    record = svc.set_status(key_id, payload.action)
    return {
        "key_id": record.key_id,
        "status": record.status,
        "last_used_at": record.last_used_at,
    }


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(key_id: str) -> Response:
    """Soft-delete: revoke the key and invalidate its sessions.

    We intentionally do not hard-delete rows so the audit trail
    (``usage_events.api_key_id``) keeps resolving even after the key is
    gone.  To re-use the same label later, create a new key with the
    same ``key_id`` — revoked rows can be re-activated via PATCH if the
    operator only wanted a temporary lockout.
    """
    svc = _service()
    svc.set_status(key_id, "revoke")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
