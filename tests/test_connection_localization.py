"""Localized connection pages preserve request guards and secret isolation."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from social_mcp.threads.connection.http import ConnectionEndpoints
from social_mcp.threads.connection.localization import (
    CATALOGS,
    LANGUAGE_COOKIE,
    choose_language,
)
from social_mcp.threads.connection.views import connected_page, form_page
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ThreadsProfile

PATH = "/connect/synthetic-session"
ORIGIN = "http://127.0.0.1:12345"
TOKEN = "synthetic-localization-secret"


def test_catalogs_have_matching_keys_and_string_values():
    expected = set(CATALOGS["en"])
    for catalog in CATALOGS.values():
        assert set(catalog) == expected
        assert all(isinstance(value, str) and value for value in catalog.values())
        assert catalog["connected"].count("{username}") == 1


@pytest.mark.parametrize(
    "cookie, header, expected",
    [
        (None, "", "en"),
        (None, "ru-RU,ru;q=0.9,en;q=0.8", "ru"),
        (None, "en;q=0.1,ru;q=0.9", "ru"),
        (None, "de,ru;q=0.7,en;q=0.1", "ru"),
        (None, "RU-ru", "ru"),
        (None, "ru;q=0,en;q=0.2", "en"),
        (None, "ru;q=bad,en", "en"),
        (None, "ru;q=NaN,en", "en"),
        (None, "ru;q=2,en", "en"),
        (None, "de,*;q=0.5", "en"),
        ("en", "ru", "en"),
        ("ru", "en", "ru"),
        ("../../secret", "ru", "ru"),
    ],
)
def test_browser_language_selection(cookie, header, expected):
    assert choose_language(cookie, header) == expected


@pytest.mark.parametrize("language", sorted(CATALOGS))
def test_localized_rendering_keeps_escaping_and_csp(language):
    response = form_page(PATH, '<script>alert("secret")</script>', language=language)
    html = response.body.decode()
    assert f'<html lang="{language}">' in html
    assert response.headers["content-language"] == language
    assert "Accept-Language" in response.headers["vary"]
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert f'data-show="{CATALOGS[language]["show"]}"' in html
    assert f'data-checking="{CATALOGS[language]["checking"]}"' in html
    assert 'id="connection-form"' in html
    assert f'action="{PATH}/language"' in html
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    profile = ThreadsProfile(id="123", username="<script>example</script>")
    result = connected_page(profile, language=language).body.decode()
    assert CATALOGS[language]["return_to_chat"] in result
    assert "&lt;script&gt;example&lt;/script&gt;" in result


def test_language_switch_is_persisted_and_never_submits_a_token():
    connect = AsyncMock(return_value=ThreadsProfile(id="123", username="example"))
    app = ConnectionEndpoints(
        path=PATH, origin=ORIGIN, connect=connect, closed=lambda: False
    ).app

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            first = await client.get(PATH, headers={"Accept-Language": "ru-RU"})
            assert CATALOGS["ru"]["headline"] in first.text
            switched = await client.post(
                f"{PATH}/language", data={"language": "en"}, headers={"Origin": ORIGIN}
            )
            assert switched.status_code == 303 and switched.headers["location"] == PATH
            assert "httponly" in switched.headers["set-cookie"].lower()
            assert "samesite=strict" in switched.headers["set-cookie"].lower()
            connect.assert_not_awaited()
            chosen = await client.get(PATH, headers={"Accept-Language": "ru"})
            assert chosen.headers["content-language"] == "en"
            client.cookies.set(LANGUAGE_COOKIE, "ru", domain="127.0.0.1", path="/")
            result = await client.post(
                PATH, data={"token": TOKEN}, headers={"Origin": ORIGIN}
            )
            assert result.status_code == 200
            assert "Подключён @example" in result.text
            assert TOKEN not in result.text

    asyncio.run(check())
    connect.assert_awaited_once_with(TOKEN)


@pytest.mark.parametrize(
    "case,status",
    [
        ("foreign", 403),
        ("query", 400),
        ("unknown", 400),
        ("duplicate", 400),
        ("token", 400),
        ("format", 415),
        ("closed", 410),
        ("large", 413),
    ],
)
def test_language_route_preserves_guards(case, status):
    connect = AsyncMock()
    app = ConnectionEndpoints(
        path=PATH, origin=ORIGIN, connect=connect, closed=lambda: case == "closed"
    ).app

    async def check():
        headers = {
            "Origin": ORIGIN,
            "Accept-Language": "ru",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = "language=en"
        url = f"{PATH}/language"
        if case == "foreign":
            headers["Origin"] = "https://evil.example"
        if case == "query":
            url += "?language=en"
        if case == "unknown":
            body = "language=de"
        if case == "duplicate":
            body = "language=en&language=ru"
        if case == "token":
            body = f"language=ru&token={TOKEN}"
        if case == "format":
            headers["Content-Type"] = "application/json"
        if case == "large":
            body = "language=" + "x" * 9000
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.post(url, content=body, headers=headers)
            assert response.status_code == status
            assert response.headers["content-language"] == "ru"
            assert TOKEN not in response.text
            assert "set-cookie" not in response.headers

    asyncio.run(check())
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    "kind,expected,status",
    [
        ("token", "invalid_token", 400),
        ("service", "invalid_connection", 400),
        ("crash", "connection_failed", 500),
        ("unknown", "not_found", 404),
    ],
)
def test_errors_follow_selected_language_without_raw_service_details(
    kind, expected, status
):
    connect = AsyncMock(
        side_effect=ThreadsError(TOKEN) if kind == "service" else RuntimeError(TOKEN)
    )
    app = ConnectionEndpoints(
        path=PATH, origin=ORIGIN, connect=connect, closed=lambda: False
    ).app

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
            cookies={LANGUAGE_COOKIE: "ru"},
        ) as client:
            response = (
                await client.get("/missing")
                if kind == "unknown"
                else await client.post(
                    PATH,
                    data={"token": "" if kind == "token" else TOKEN},
                    headers={"Origin": ORIGIN},
                )
            )
            assert response.status_code == status
            assert CATALOGS["ru"][expected] in response.text
            assert TOKEN not in response.text

    asyncio.run(check())
