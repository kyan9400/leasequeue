from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from leasequeue.api import router, static_dir
from leasequeue.config import Settings, settings
from leasequeue.database import Database
from leasequeue.service import QueueError, QueueService


def create_app(app_settings: Settings | None = None) -> FastAPI:
    active_settings = app_settings or settings
    database = Database(active_settings.database_path)
    service = QueueService(database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.initialize()
        yield

    docs_enabled = active_settings.environment != "production"
    app = FastAPI(
        title="LeaseQueue",
        version="1.0.0",
        description="Durable background jobs with explicit lease ownership and recovery.",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.queue_service = service
    app.state.settings = active_settings
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def request_guards(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdecimal()
            and int(content_length) > active_settings.max_request_bytes
        ):
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "request_too_large",
                        "message": "Request body exceeds the configured limit",
                    }
                },
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        return response

    @app.exception_handler(QueueError)
    async def queue_error_handler(_: Request, error: QueueError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(part) for part in item["loc"][1:]), "message": item["msg"]}
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    return app


app = create_app()
