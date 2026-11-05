import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from social_mcp.accounts import get_account, save_account
from social_mcp.threads import ThreadsService, tokens
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import RefreshedToken, ThreadsProfile, TokenInfo

NOW = 1_800_000_000
DAY = 86400
TOKEN = "synthetic-old-credential"
NEW_TOKEN = "synthetic-new-credential"
PROFILE = ThreadsProfile(id="123", username="example")


def info(**changes):
    values = dict(
        user_id="123",
        is_valid=True,
        issued_at=NOW - 59 * DAY,
        expires_at=NOW + DAY,
        scopes=["threads_basic"],
    )
    values.update(changes)
    return TokenInfo(**values)


def store(path, *, expiry: int | None = NOW + DAY):
    save_account(
        path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=NOW - DAY,
        expires_at=expiry,
    )


@pytest.fixture(autouse=True)
def clock_and_profile(monkeypatch):
    monkeypatch.setattr("social_mcp.threads.tokens.time.time", lambda: NOW)
    fetch = AsyncMock(return_value=PROFILE)
    monkeypatch.setattr("social_mcp.threads.service.fetch_profile", fetch)
    return fetch


def test_connect_saves_meta_expiry_not_import_date(db_path, monkeypatch):
    inspect = AsyncMock(return_value=info())
    monkeypatch.setattr("social_mcp.threads.tokens.inspect_token", inspect)
    asyncio.run(ThreadsService(db_path).connect(TOKEN))
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.expires_at == NOW + DAY
    assert account.obtained_at == NOW
    inspect.assert_awaited_once_with(TOKEN)


@pytest.mark.parametrize(
    "changes",
    [
        {"is_valid": False},
        {"expires_at": NOW},
        {"user_id": "999"},
        {"scopes": []},
        {"data_access_expires_at": NOW - 1},
    ],
)
def test_invalid_metadata_cannot_replace_previous_connection(
    db_path, monkeypatch, changes
):
    store(db_path)
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(return_value=info(**changes)),
    )
    with pytest.raises(ThreadsError):
        asyncio.run(ThreadsService(db_path).connect(NEW_TOKEN))
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == TOKEN


def test_data_access_expiry_limits_usable_time(db_path, monkeypatch):
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(return_value=info(data_access_expires_at=NOW + 100)),
    )
    asyncio.run(ThreadsService(db_path).connect(TOKEN))
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.expires_at == NOW + 100


@pytest.mark.parametrize(
    ("issued", "expires"), [(NOW - 100, NOW + 3500), (NOW - 100, NOW + 60 * DAY)]
)
def test_short_and_young_tokens_are_not_refreshed(
    db_path, monkeypatch, issued, expires
):
    store(db_path, expiry=None)
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(
            return_value=info(
                issued_at=issued, expires_at=expires, data_access_expires_at=NOW + 3600
            )
        ),
    )
    refresh = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.tokens.refresh_token", refresh)
    asyncio.run(ThreadsService(db_path).get_profile("123"))
    refresh.assert_not_awaited()
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.expires_at == min(expires, NOW + 3600)


def test_expired_token_is_not_sent_to_meta(db_path, monkeypatch, clock_and_profile):
    store(db_path, expiry=NOW)
    inspect = AsyncMock(return_value=info(expires_at=NOW))
    refresh = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.tokens.inspect_token", inspect)
    monkeypatch.setattr("social_mcp.threads.tokens.refresh_token", refresh)
    with pytest.raises(ThreadsError, match="expired"):
        asyncio.run(ThreadsService(db_path).get_profile("123"))
    inspect.assert_not_awaited()
    refresh.assert_not_awaited()
    clock_and_profile.assert_not_awaited()


def test_refresh_persists_verified_token_and_expiry_together(
    db_path, monkeypatch, clock_and_profile
):
    store(db_path)
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(side_effect=[info(), info(issued_at=NOW, expires_at=NOW + 60 * DAY)]),
    )
    refresh = AsyncMock(
        return_value=RefreshedToken(
            access_token=SecretStr(NEW_TOKEN), expires_in=60 * DAY
        )
    )
    monkeypatch.setattr("social_mcp.threads.tokens.refresh_token", refresh)
    asyncio.run(ThreadsService(db_path).get_profile("123"))
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert (
        account.access_token.get_secret_value(),
        account.expires_at,
        account.obtained_at,
    ) == (
        NEW_TOKEN,
        NOW + 60 * DAY,
        NOW,
    )
    refresh.assert_awaited_once_with(TOKEN)
    clock_and_profile.assert_awaited_once_with(NEW_TOKEN)


@pytest.mark.parametrize("stage", ["refresh", "inspect", "new_inspect", "wrong_owner"])
def test_refresh_failure_preserves_old_token_and_defers_retry(
    db_path, monkeypatch, clock_and_profile, caplog, stage
):
    store(db_path)
    inspect = AsyncMock(return_value=info())
    refresh = AsyncMock(
        return_value=RefreshedToken(
            access_token=SecretStr(NEW_TOKEN), expires_in=60 * DAY
        )
    )
    if stage == "refresh":
        refresh.side_effect = ThreadsTransientError(TOKEN)
    elif stage == "inspect":
        inspect.side_effect = ThreadsTransientError(TOKEN)
    elif stage == "new_inspect":
        inspect.side_effect = [info(), ThreadsTransientError(NEW_TOKEN)]
    else:
        inspect.side_effect = [
            info(),
            info(user_id="999", issued_at=NOW, expires_at=NOW + 60 * DAY),
        ]
    monkeypatch.setattr("social_mcp.threads.tokens.inspect_token", inspect)
    monkeypatch.setattr("social_mcp.threads.tokens.refresh_token", refresh)

    async def check():
        service = ThreadsService(db_path)
        await service.get_profile("123")
        calls = inspect.await_count
        await service.get_profile("123")
        assert inspect.await_count == calls

    asyncio.run(check())
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert (account.access_token.get_secret_value(), account.expires_at) == (
        TOKEN,
        NOW + DAY,
    )
    assert [call.args for call in clock_and_profile.await_args_list] == [
        (TOKEN,),
        (TOKEN,),
    ]
    assert TOKEN not in caplog.text
    assert NEW_TOKEN not in caplog.text


def test_two_service_instances_refresh_only_once(
    db_path, monkeypatch, clock_and_profile
):
    store(db_path)

    async def check():
        entered = asyncio.Event()
        release = asyncio.Event()

        def inspect(token):
            return (
                info()
                if token == TOKEN
                else info(issued_at=NOW, expires_at=NOW + 60 * DAY)
            )

        async def refresh(_token):
            entered.set()
            await release.wait()
            return RefreshedToken(
                access_token=SecretStr(NEW_TOKEN), expires_in=60 * DAY
            )

        refresh_mock = AsyncMock(side_effect=refresh)
        monkeypatch.setattr(
            "social_mcp.threads.tokens.inspect_token", AsyncMock(side_effect=inspect)
        )
        monkeypatch.setattr("social_mcp.threads.tokens.refresh_token", refresh_mock)
        first = asyncio.create_task(ThreadsService(db_path).get_profile("123"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(ThreadsService(db_path).get_profile("123"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)
        refresh_mock.assert_awaited_once()

    asyncio.run(check())
    assert [call.args for call in clock_and_profile.await_args_list] == [
        (NEW_TOKEN,),
        (NEW_TOKEN,),
    ]


def test_reconnect_wins_over_stale_metadata_write(
    db_path, monkeypatch, clock_and_profile
):
    store(db_path, expiry=None)
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(return_value=info(expires_at=NOW + 60 * DAY)),
    )
    original = tokens.replace_credentials

    def race(*args, **kwargs):
        save_account(
            db_path,
            platform="threads",
            account_id="123",
            username="reconnected",
            access_token=NEW_TOKEN,
            obtained_at=NOW,
            expires_at=NOW + 60 * DAY,
        )
        return original(*args, **kwargs)

    monkeypatch.setattr("social_mcp.threads.tokens.replace_credentials", race)
    asyncio.run(ThreadsService(db_path).get_profile("123"))
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == NEW_TOKEN
    clock_and_profile.assert_awaited_once_with(NEW_TOKEN)


def test_cancelled_connection_keeps_lock_until_writer_finishes(db_path, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    write = tokens.save_account
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token", AsyncMock(return_value=info())
    )

    def slow_write(*args, **kwargs):
        if kwargs["access_token"] == TOKEN:
            started.set()
            assert release.wait(3)
        write(*args, **kwargs)

    monkeypatch.setattr("social_mcp.threads.tokens.save_account", slow_write)

    async def check():
        second_ready = asyncio.Event()

        async def fetch(token):
            if token == NEW_TOKEN:
                second_ready.set()
            return PROFILE

        monkeypatch.setattr("social_mcp.threads.service.fetch_profile", fetch)
        first = asyncio.create_task(ThreadsService(db_path).connect(TOKEN))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(ThreadsService(db_path).connect(NEW_TOKEN))
            await asyncio.wait_for(second_ready.wait(), 2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(first), timeout=0.2)
            assert not first.done(), "Cancellation must wait for the outstanding write"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            if second is not None:
                await second

    asyncio.run(check())
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == NEW_TOKEN


def test_two_processes_serialize_refresh(db_path, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    store(db_path)
    marker = tmp_path / "refreshes.txt"
    code = """
import asyncio
import sys
from pathlib import Path
from social_mcp.threads import ThreadsService
from social_mcp.threads import tokens, service
from social_mcp.threads.schemas import TokenInfo, RefreshedToken, ThreadsProfile
now = 1800000000
tokens.time.time = lambda: now
async def inspect(token):
    old = token == 'synthetic-old-credential'
    return TokenInfo(user_id='123', is_valid=True, issued_at=now-59*86400 if old else now, expires_at=now+86400 if old else now+60*86400, scopes=['threads_basic'])
async def refresh(token):
    with Path(sys.argv[2]).open('a') as log:
        log.write('refresh\\n')
    await asyncio.sleep(.2)
    return RefreshedToken(access_token='synthetic-new-credential', expires_in=60*86400)
async def fetch(token):
    assert token == 'synthetic-new-credential'
    return ThreadsProfile(id='123', username='example')
tokens.inspect_token = inspect
tokens.refresh_token = refresh
service.fetch_profile = fetch
print('ready', flush=True)
input()
asyncio.run(ThreadsService(Path(sys.argv[1])).get_profile('123'))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(db_path), str(marker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.flush()
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stdout + stderr
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
    assert marker.read_text().splitlines() == ["refresh"]


def test_fresh_known_token_does_not_query_metadata(db_path, monkeypatch):
    store(db_path, expiry=NOW + 30 * DAY)
    inspect = AsyncMock(return_value=info())
    monkeypatch.setattr("social_mcp.threads.tokens.inspect_token", inspect)
    asyncio.run(ThreadsService(db_path).get_profile("123"))
    inspect.assert_not_awaited()
