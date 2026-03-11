from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.admin_api.routes import router as admin_router
from app.errors import AppError, to_error_response
from app.public_api.routes import router as public_router

app = FastAPI(title="PISCO-API", version="0.1.0")
app.include_router(public_router)
app.include_router(admin_router)


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
