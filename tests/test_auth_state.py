import asyncio
import json
import os

import pytest

from app.browser.auth_state import (
    get_auth_state_path,
    load_auth_cookies,
    restore_auth_cookies,
    save_auth_state,
)


class _FakeContext:
    def __init__(self) -> None:
        self.added_cookies: list[dict] = []

    async def cookies(self, urls=None) -> list[dict]:
        return []  # empty profile: saved cookies may be restored

    async def storage_state(self) -> dict:
        return {
            "cookies": [
                {
                    "name": "session-cookie",
                    "value": "test-only",
                    "domain": "example.test",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added_cookies = cookies


def test_saves_session_cookies_to_private_auth_state_file(tmp_path) -> None:
    context = _FakeContext()

    auth_path = asyncio.run(save_auth_state(context, tmp_path / "profile"))

    saved_state = json.loads(auth_path.read_text(encoding="utf-8"))
    assert auth_path == get_auth_state_path(tmp_path / "profile")
    assert saved_state["cookies"][0]["expires"] == -1
    assert load_auth_cookies(tmp_path / "profile") == saved_state["cookies"]
    if os.name == "posix":
        assert auth_path.stat().st_mode & 0o777 == 0o600


def test_restores_saved_cookies_into_browser_context(tmp_path) -> None:
    profile_path = tmp_path / "profile"
    asyncio.run(save_auth_state(_FakeContext(), profile_path))
    context = _FakeContext()

    restored = asyncio.run(restore_auth_cookies(context, profile_path))

    assert restored
    assert context.added_cookies == load_auth_cookies(profile_path)
    assert context.added_cookies[0]["expires"] == -1


def test_missing_state_file_has_no_cookies_to_restore(tmp_path) -> None:
    assert not asyncio.run(restore_auth_cookies(_FakeContext(), tmp_path / "missing"))


def test_malformed_state_file_fails_explicitly(tmp_path) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    get_auth_state_path(profile_path).write_text(
        json.dumps({"cookies": "not-a-list"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid Outlook cookie state format"):
        load_auth_cookies(profile_path)


def test_restore_skips_when_profile_already_signed_in(tmp_path) -> None:
    import asyncio
    import json as _json

    from app.browser import auth_state

    (tmp_path / "auth.json").write_text(_json.dumps({"cookies": [{"name": "old"}]}))

    class Context:
        def __init__(self, existing):
            self.existing, self.added = existing, []

        async def cookies(self, urls):
            return self.existing

        async def add_cookies(self, cookies):
            self.added += cookies

    signed_in = Context([{"name": "fresh"}])
    assert asyncio.run(auth_state.restore_auth_cookies(signed_in, tmp_path)) is False
    assert signed_in.added == []  # fresh profile cookies are never overwritten

    empty = Context([])
    assert asyncio.run(auth_state.restore_auth_cookies(empty, tmp_path)) is True
    assert empty.added == [{"name": "old"}]
