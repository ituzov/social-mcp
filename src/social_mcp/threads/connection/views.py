"""Connection HTML pages with escaped dynamic text."""

import secrets
from importlib.resources import files

from jinja2 import Environment, PackageLoader, StrictUndefined, Template
from starlette.responses import HTMLResponse

from social_mcp.threads.connection.localization import CATALOGS, LANGUAGES, translate
from social_mcp.threads.schemas import ThreadsProfile

TEMPLATES = Environment(
    loader=PackageLoader(__package__, "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)
FORM = TEMPLATES.get_template("connection.html")
MESSAGE = TEMPLATES.get_template("message.html")
ASSETS = files(__package__).joinpath("assets")
STYLES = ASSETS.joinpath("connection.css").read_text(encoding="utf-8")
SCRIPT = ASSETS.joinpath("connection.js").read_text(encoding="utf-8")
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


def _render(
    template: Template, status: int, *, language: str = "en", **values: str
) -> HTMLResponse:
    if language not in CATALOGS:
        language = "en"
    nonce = secrets.token_urlsafe(24)
    html = template.render(
        **values,
        nonce=nonce,
        styles=STYLES,
        script=SCRIPT,
        language=language,
        t=CATALOGS[language],
        languages=LANGUAGES,
    )
    policy = (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'"
    )
    return HTMLResponse(
        html,
        status_code=status,
        headers={
            **HEADERS,
            "Content-Security-Policy": policy,
            "Content-Language": language,
            "Vary": "Cookie, Accept-Language",
        },
    )


def form_page(
    path: str, message: str = "", status: int = 200, *, language: str = "en"
) -> HTMLResponse:
    return _render(FORM, status, path=path, message=message, language=language)


def message_page(message: str, status: int, *, language: str = "en") -> HTMLResponse:
    return _render(
        MESSAGE,
        status,
        title=translate(language, "connection_title"),
        language=language,
        message=message,
        state="error",
        symbol="!",
    )


def connected_page(profile: ThreadsProfile, *, language: str = "en") -> HTMLResponse:
    return _render(
        MESSAGE,
        200,
        title=translate(language, "connected").format(username=profile.username),
        message=translate(language, "return_to_chat"),
        language=language,
        state="success",
        symbol="✓",
    )
