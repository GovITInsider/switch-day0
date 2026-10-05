from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.db import db_session, init_db
from app.errors import AppError, AuthRequired, Forbidden
from app.routes import register
from app.settings import load_settings
from app.services import ensure_admin, ensure_seed
from app.stored_secrets import ensure_secret_catalog
from app.ui import login_redirect, render_page

STATIC = Path(__file__).resolve().parent / "web" / "static"


def create_app():
    settings = load_settings()
    init_db(settings.db_path)
    with db_session(settings) as conn:
        ensure_admin(conn, settings)
        ensure_seed(conn)
        ensure_secret_catalog(conn)

    app = FastAPI(title="Switch Day-0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 12,
        session_cookie="switch_day0",
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthRequired)
    async def need_login(request, exc):
        return login_redirect(exc.next_url)

    @app.exception_handler(Forbidden)
    async def forbidden(request, exc):
        return render_page(
            request,
            "error.html",
            status=403,
            title="Not allowed",
            message="Your role does not allow that.",
        )

    @app.exception_handler(AppError)
    async def show_app_error(request, exc):
        return render_page(
            request,
            "error.html",
            status=exc.status,
            title="Try again",
            message=exc.message,
        )

    register(app)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app
