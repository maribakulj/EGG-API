"""Canonical application error type and JSON renderer.

All user-visible failures flow through :class:`AppError`, which carries a
machine-readable ``code``, a human ``message``, a ``details`` mapping, and
the HTTP ``status_code`` to emit. :func:`to_error_response` renders the
payload in the shape specified in SPECS.md §19 and always includes the
request id so logs and client traces can be joined.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.logging.request_context import get_request_id


class AppError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, object] | None = None, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code


def to_error_response(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "request_id": get_request_id(request),
            }
        },
    )
