from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin_api.routes import router as admin_router
from app.admin_ui.routes import router as admin_ui_router
from app.dependencies import container
from app.errors import AppError, to_error_response
from app.logging.request_context import get_request_id
from app.public_api.routes import router as public_router

app = FastAPI(title="PISCO-API", version="0.1.0")
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(admin_ui_router)
app.mount("/admin-static", StaticFiles(directory=Path(__file__).parent / "admin_ui" / "static"), name="admin-static")


@app.middleware("http")
async def usage_audit_middleware(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)

    subject = request.headers.get("x-api-key") or (request.client.host if request.client else "anonymous")
    container.store.log_usage_event(
        request_id=get_request_id(request),
        endpoint=request.url.path,
        method=request.method,
        status_code=response.status_code,
        api_key_id=request.headers.get("x-api-key"),
        subject=str(subject),
        latency_ms=int((time.monotonic() - started) * 1000),
        error_code=None,
    )
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return to_error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_parameter",
                "message": "Validation error",
                "details": {"errors": exc.errors()},
                "request_id": request.headers.get("x-request-id", "generated"),
            }
        },
    )


@app.get("/v1/openapi.json")
def openapi_json() -> dict[str, object]:
    return app.openapi()
