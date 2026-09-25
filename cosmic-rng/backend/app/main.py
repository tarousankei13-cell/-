"""FastAPI application factory."""
from __future__ import annotations

import logging
import logging.handlers
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.routing import Mount

from .config import BASE_DIR, get_settings
from .content.registry import get_registry
from .core.errors import install_error_handlers
from .core.pubsub import bus
from .db import dispose_engine, session_scope
from .ws.hub import hub

log = logging.getLogger("cosmic")
FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"


def setup_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    if getattr(root, "_cosmic_configured", False):
        return
    root.setLevel(s.log_level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    h: logging.Handler = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    root.addHandler(h)
    if s.log_file:
        Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(s.log_file, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    root._cosmic_configured = True  # type: ignore[attr-defined]


async def run_once(db, marker: str, fn) -> None:
    """Run a data repair exactly once per database, recorded in game_settings.

    For fixes that must touch rows written before the code changed — here, best
    item records that predate granted and generated items counting — and that
    should not cost every subsequent boot a full table scan.
    """
    from .models import GameSetting

    if await db.get(GameSetting, marker) is not None:
        return
    result = await fn(db)
    db.add(GameSetting(key=marker, value={"done": True, "result": result}))
    await db.commit()
    log.info("one-time repair %s: %s", marker, result)


async def _recompute_best_items(db):
    from .services.stats import recompute_best_items

    return await recompute_best_items(db)


async def _generated_names_ja(db):
    from .services.stats import backfill_generated_names_ja

    return await backfill_generated_names_ja(db)


async def ensure_content() -> None:
    from .content.seeder import seed
    from .models import Rarity

    async with session_scope() as db:
        if (await db.execute(select(Rarity).limit(1))).scalar_one_or_none() is None:
            log.warning("empty database detected — seeding initial content")
            await seed(db)
    async with session_scope() as db:
        from .content.seeder import backfill_names_ja

        await backfill_names_ja(db)
    async with session_scope() as db:
        await get_registry().reload(db)
    async with session_scope() as db:
        await run_once(db, "backfill.best_items_v1", _recompute_best_items)
    async with session_scope() as db:
        await run_once(db, "backfill.generated_names_ja_v1", _generated_names_ja)
    async with session_scope() as db:
        from .services.users import ensure_admin_account

        status = await ensure_admin_account(db)
        if status:
            log.info("%s", status)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    s = get_settings()
    await ensure_content()
    bus.subscribe(hub.dispatch)
    await bus.start()
    from .tasks.scheduler import scheduler

    if s.run_scheduler:
        await scheduler.start()
    log.info("Cosmic RNG started (env=%s, bus=%s)", s.environment, s.event_bus)
    try:
        yield
    finally:
        if s.run_scheduler:
            await scheduler.stop()
        await bus.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Cosmic RNG",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if s.is_dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if s.is_dev else None,
    )
    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        start = time.perf_counter()
        request.state.request_id = secrets.token_hex(6)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self' ws: wss:; font-src 'self' data:; media-src 'self' blob:; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com",
            )
        dur = (time.perf_counter() - start) * 1000
        if dur > 1500:
            log.warning("slow request %s %s %.0fms", request.method, request.url.path, dur)
        return response

    from .api import admin, auth, economy, game
    from .ws import routes as ws_routes

    app.include_router(auth.router)
    app.include_router(game.router)
    app.include_router(economy.router)
    app.include_router(admin.router)
    app.include_router(ws_routes.router)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        async with session_scope() as db:
            await db.execute(select(1))
        reg = get_registry()
        return {"ok": True, "content_version": reg.snap.version if reg.loaded else None}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def api_not_found(path: str) -> JSONResponse:
        return JSONResponse({"error": {"code": "not_found", "message": "Not Found"}}, status_code=404)

    # Optional: serve the built SPA directly (Nginx normally does this in production).
    if FRONTEND_DIST.exists():

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            candidate = (FRONTEND_DIST / path).resolve()
            if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
                headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in f"/{path}" else {}
                return FileResponse(candidate, headers=headers)
            return HTMLResponse(_spa_shell(), headers={"Cache-Control": "no-cache"})

    base = get_settings().base_path
    if not base:
        return app

    # Served under a sub-path (shared hosting, a reverse proxy that does not strip
    # its prefix). Mounting keeps every route relative while the browser sees the
    # full path; the SPA shell carries the same prefix in its <base href>.
    outer = Starlette(routes=[Mount(base, app=app)])
    outer.state.inner = app
    outer.router.lifespan_context = app.router.lifespan_context
    log.info("mounted under %s", base)
    return outer


_shell_cache: tuple[float, str] | None = None


def _spa_shell() -> str:
    """index.html with a <base href> so relative asset and route URLs resolve
    correctly whether the app owns the origin or sits under a sub-path.

    Re-read when the file changes, so deploying a new frontend build does not
    keep serving a shell that points at the previous bundle's hashed assets.
    """
    global _shell_cache
    index = FRONTEND_DIST / "index.html"
    mtime = index.stat().st_mtime
    if _shell_cache is not None and _shell_cache[0] == mtime:
        return _shell_cache[1]
    html = index.read_text(encoding="utf-8")
    if "<base " not in html:
        html = html.replace("<head>", f'<head>\n    <base href="{get_settings().base_path}/">', 1)
    _shell_cache = (mtime, html)
    return html


app = create_app()
