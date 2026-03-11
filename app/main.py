from __future__ import annotations

import time

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
app.mount("/admin-static", StaticFiles(directory="app/admin_ui/static"), name="admin-static")
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(admin_ui_router)


@app.middleware("http")
async def usage_event_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    request_id = get_request_id(request)
    request.state.request_id = request_id
    request.state.error_code = None
    request.state.api_key_id = getattr(request.state, "api_key_id", None)

    try:
        response = await call_next(request)
        status_code = response.status_code
    except AppError as exc:
        request.state.error_code = exc.code
        status_code = exc.status_code
        raise
    except Exception:  # noqa: BLE001
        request.state.error_code = "internal_error"
        status_code = 500
        raise
    finally:
        latency_ms = int((time.perf_counter() - start) * 1000)
        subject = request.state.api_key_id or (request.client.host if request.client else "anonymous")
        container.store.log_usage_event(
            request_id=request_id,
            endpoint=request.url.path,
            method=request.method,
            status_code=status_code,
            api_key_id=request.state.api_key_id,
            subject=str(subject),
            latency_ms=latency_ms,
            error_code=request.state.error_code,
        )

    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    request.state.error_code = exc.code
    return to_error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request.state.error_code = "invalid_parameter"
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_parameter",
                "message": "Validation error",
                "details": {"errors": exc.errors()},
                "request_id": get_request_id(request),
            }
        },
    )


@app.get("/v1/openapi.json")
def openapi_json() -> dict[str, object]:
    return app.openapi()
