import asyncio
import re
from unittest.mock import AsyncMock, create_autospec
from urllib.parse import urlsplit

import httpx
import pytest
from jinja2 import UndefinedError

from social_mcp.accounts import get_account
from social_mcp.threads import ThreadsError, ThreadsService
from social_mcp.threads.connection import ConnectionPage
from social_mcp.threads.connection import http as connection_http
from social_mcp.threads.connection import views as connection_views
from social_mcp.threads.connection.views import form_page
from social_mcp.threads.schemas import ThreadsProfile

pytestmark = pytest.mark.usefixtures("verified_token")

TOKEN = "synthetic-form-secret"
PROFILE = ThreadsProfile(id="123", username="example")


def test_missing_template_value_fails_instead_of_rendering_blank(monkeypatch):
    template = connection_views.TEMPLATES.from_string("{{ missing_styles }}")
    monkeypatch.setattr("social_mcp.threads.connection.views.FORM", template)
    with pytest.raises(UndefinedError, match="missing_styles"):
        form_page("/connect/test")


def test_form_assets_use_fresh_csp_nonce_and_message_cannot_inject_script():
    response = form_page("/connect/test", "</style><script>alert('x')</script>")
    html = response.body.decode()
    nonces = re.findall(r'<(?:style|script) nonce="([^"]+)"', html)
    assert len(nonces) == 2
    assert len(set(nonces)) == 1
    policy = response.headers["content-security-policy"]
    assert f"style-src 'nonce-{nonces[0]}'" in policy
    assert f"script-src 'nonce-{nonces[0]}'" in policy
    assert "default-src 'none'" in policy
    assert "unsafe-inline" not in policy
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert ".brand-mark svg" in html
    assert "visibility.addEventListener" in html
    assert (
        nonces[0] not in form_page("/connect/test").headers["content-security-policy"]
    )


def origin(page):
    url = urlsplit(page.url)
    return f"{url.scheme}://{url.netloc}"


def test_real_http_form_saves_account_and_server_stops(
    db_path, monkeypatch, caplog, capsys
):
    fetch = AsyncMock(return_value=PROFILE)
    monkeypatch.setattr("social_mcp.threads.service.fetch_profile", fetch)
    caplog.set_level(5)  # Even ASGI trace logging must not reveal the form body.

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            async with ConnectionPage(ThreadsService(db_path)) as page:
                form = await client.get(page.url)
                assert form.status_code == 200
                assert 'type="password"' in form.text
                assert 'method="post"' in form.text
                assert form.headers["cache-control"] == "no-store"
                # no-referrer turns native form POST Origin into null in browsers.
                assert form.headers["referrer-policy"] == "same-origin"
                assert (
                    "frame-ancestors 'none'" in form.headers["content-security-policy"]
                )
                response = await client.post(
                    page.url, data={"token": TOKEN}, headers={"Origin": origin(page)}
                )
                assert response.status_code == 200
                assert "@example" in response.text
                assert TOKEN not in response.text
                assert (await page.wait()).id == "123"
                async with asyncio.timeout(2):
                    await page.wait_closed()
                with pytest.raises(httpx.ConnectError):
                    await client.get(page.url)
            with pytest.raises(httpx.ConnectError):
                await client.get(page.url)

    asyncio.run(check())
    fetch.assert_awaited_once_with(TOKEN)
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.access_token.get_secret_value() == TOKEN
    assert TOKEN not in caplog.text
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("host", 403),
        ("origin", 403),
        ("no_origin", 403),
        ("null_origin", 403),
        ("query", 400),
        ("path", 404),
        ("content_type", 415),
        ("oversized", 413),
        ("duplicate", 400),
        ("empty", 400),
    ],
)
def test_invalid_submission_never_calls_service(change, expected):
    connect = AsyncMock(return_value=PROFILE)

    async def check():
        service = create_autospec(
            ThreadsService, instance=True, spec_set=True, connect=connect
        )
        async with ConnectionPage(service) as page:
            headers = {"Origin": origin(page)}
            url = page.url
            data = {"token": TOKEN}
            if change == "host":
                headers["Host"] = "evil.example"
            elif change == "origin":
                headers["Origin"] = "https://evil.example"
            elif change == "no_origin":
                headers.clear()
            elif change == "null_origin":
                headers["Origin"] = "null"
            elif change == "query":
                url += "?token=forbidden"
            elif change == "path":
                url = origin(page) + "/connect/unknown"
            elif change == "content_type":
                headers["Content-Type"] = "application/json"
            elif change == "oversized":
                data = {"token": "x" * 9000}
            elif change == "empty":
                data = {"token": " "}
            async with httpx.AsyncClient(trust_env=False) as client:
                if change == "duplicate":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    response = await client.post(
                        url, content="token=one&token=two", headers=headers
                    )
                else:
                    response = await client.post(url, data=data, headers=headers)
            assert response.status_code == expected
            assert TOKEN not in response.text

    asyncio.run(check())
    connect.assert_not_awaited()


@pytest.mark.parametrize("failure", [ThreadsError(TOKEN), RuntimeError(TOKEN)])
def test_error_is_not_echoed_and_user_can_retry(failure, caplog):
    connect = AsyncMock(side_effect=[failure, PROFILE])

    async def check():
        service = create_autospec(
            ThreadsService, instance=True, spec_set=True, connect=connect
        )
        async with ConnectionPage(service) as page:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    page.url, data={"token": TOKEN}, headers={"Origin": origin(page)}
                )
                assert response.status_code in (400, 500)
                assert TOKEN not in response.text
                assert 'type="password"' in response.text
                response = await client.post(
                    page.url, data={"token": TOKEN}, headers={"Origin": origin(page)}
                )
                assert response.status_code == 200
                assert (await page.wait()).id == "123"

    asyncio.run(check())
    assert connect.await_count == 2
    assert TOKEN not in caplog.text


def test_simultaneous_posts_only_connect_once(monkeypatch):
    async def check():
        entered = asyncio.Event()
        release = asyncio.Event()
        both_submitted = asyncio.Event()
        read_token_form = connection_http.read_token_form
        submissions = 0

        async def track_submission(request):
            nonlocal submissions
            token = await read_token_form(request)
            submissions += 1
            if submissions == 2:
                both_submitted.set()
            return token

        monkeypatch.setattr(
            "social_mcp.threads.connection.http.read_token_form", track_submission
        )

        async def connect(_token):
            entered.set()
            await release.wait()
            return PROFILE

        service = create_autospec(
            ThreadsService,
            instance=True,
            spec_set=True,
            connect=AsyncMock(side_effect=connect),
        )
        async with ConnectionPage(service) as page:
            # ASGI requests reach the same handler even when its socket starts closing.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=page.app),
                base_url=origin(page),
            ) as client:
                first = asyncio.create_task(
                    client.post(
                        page.url,
                        data={"token": TOKEN},
                        headers={"Origin": origin(page)},
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=2)
                second = asyncio.create_task(
                    client.post(
                        page.url,
                        data={"token": TOKEN},
                        headers={"Origin": origin(page)},
                    )
                )
                await asyncio.wait_for(both_submitted.wait(), timeout=2)
                release.set()
                results = await asyncio.gather(first, second)
                assert sorted(response.status_code for response in results) == [
                    200,
                    410,
                ]
                service.connect.assert_awaited_once_with(TOKEN)

    asyncio.run(check())


def test_page_waits_until_explicit_close_and_closes_socket():
    async def check():
        service = create_autospec(ThreadsService, instance=True, spec_set=True)
        async with ConnectionPage(service) as page:
            waiter = asyncio.create_task(page.wait())
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(page.url)
                assert response.status_code == 200
                assert not waiter.done()
                await page.close()
                async with asyncio.timeout(2):
                    with pytest.raises(ThreadsError, match="closed"):
                        await waiter
                with pytest.raises(httpx.ConnectError):
                    await client.get(page.url)
            service.connect.assert_not_awaited()

    asyncio.run(check())


def test_success_escapes_profile_name():
    profile = ThreadsProfile(id="123", username='<script>alert("x")</script>')

    async def check():
        service = create_autospec(ThreadsService, instance=True, spec_set=True)
        service.connect.return_value = profile
        async with ConnectionPage(service) as page:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    page.url, data={"token": TOKEN}, headers={"Origin": origin(page)}
                )
            assert "<script>" not in response.text
            assert "&lt;script&gt;" in response.text

    asyncio.run(check())
