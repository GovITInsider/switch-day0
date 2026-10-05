import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.auth import current_user
from app.constants import ROLE_LABELS
from app.errors import AppError
from app.services import format_when, month_line

WEB = Path(__file__).resolve().parent / "web"
ENV = Environment(
    loader=FileSystemLoader(str(WEB / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
ENV.filters["when"] = format_when


def flash(request, message, level="info"):
    request.session.setdefault("_flashes", []).append({"message": message, "level": level})


def csrf_token(request):
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf"] = token
    return token


def check_csrf(request, form):
    sent = form.get("csrf") if form is not None else None
    if not sent or sent != request.session.get("_csrf"):
        raise AppError("This form expired. Reload the page and try again.")


def render_page(request, name, status=200, **context):
    user = current_user(request)
    html = ENV.get_template(name).render(
        user=user,
        csrf=csrf_token(request),
        flashes=request.session.pop("_flashes", []),
        role_labels=ROLE_LABELS,
        month_line=month_line(),
        **context,
    )
    return HTMLResponse(html, status_code=status)


def redirect(url):
    return RedirectResponse(url, status_code=303)


def safe_next(value):
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def login_redirect(path):
    return redirect("/login?next=%s" % quote(path or "/", safe="/"))
