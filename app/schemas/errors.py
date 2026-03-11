from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
