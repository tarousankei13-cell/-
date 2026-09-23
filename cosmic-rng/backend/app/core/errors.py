"""Application errors and safe error responses.

Users receive a stable ``code`` and a human readable message. Internal details
are logged server side (and recorded to ``error_logs`` for the admin monitor),
never returned to the client.
"""
from __future__ import annotations

import logging
import secrets
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("cosmic.errors")


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.data = data or {}
        self.headers = headers or {}


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class FeatureLocked(AppError):
    status_code = 403
    code = "feature_locked"


def _payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


async def _record_error(request: Request, exc: BaseException, error_id: str) -> None:
    """Persist unexpected errors for the admin error monitor (best effort)."""
    try:
        from ..db import session_scope
        from ..models import ErrorLog

        user_id = getattr(request.state, "user_id", None)
        async with session_scope() as db:
            db.add(
                ErrorLog(
                    error_id=error_id,
                    path=str(request.url.path)[:256],
                    method=request.method[:8],
                    user_id=user_id,
                    exc_type=type(exc).__name__[:128],
                    message=str(exc)[:2000],
                    traceback="".join(traceback.format_exception(exc))[-8000:],
                )
            )
            await db.commit()
    except Exception:  # pragma: no cover - never let error logging raise
        log.exception("failed to record error %s", error_id)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            _payload(exc.code, exc.message, **({"data": exc.data} if exc.data else {})),
            status_code=exc.status_code,
            headers=exc.headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}.get(
            exc.status_code, "http_error"
        )
        message = exc.detail if isinstance(exc.detail, str) else code
        return JSONResponse(_payload(code, message), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for err in exc.errors()[:10]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
            fields.append({"field": loc, "message": str(err.get("msg", "invalid"))[:200]})
        return JSONResponse(_payload("validation_error", "入力内容が不正です", fields=fields), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        error_id = secrets.token_hex(6)
        log.exception("unhandled error id=%s path=%s", error_id, request.url.path)
        await _record_error(request, exc, error_id)
        try:
            from ..services.metrics import metrics

            metrics.errors += 1
        except Exception:  # pragma: no cover
            pass
        return JSONResponse(
            _payload("internal_error", "サーバーでエラーが発生しました。時間をおいて再度お試しください。", error_id=error_id),
            status_code=500,
        )
