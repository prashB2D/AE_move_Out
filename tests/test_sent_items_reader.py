import asyncio

import pytest

from app.browser import outlook_reader


def test_read_sent_items_selects_sentitems_folder(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def fake_read_inbox(
        limit: int,
        timeout_ms: int,
        allow_login: bool,
        folder: str,
    ) -> list[dict[str, object]]:
        calls.update(
            limit=limit,
            timeout_ms=timeout_ms,
            allow_login=allow_login,
            folder=folder,
        )
        return [{"subject": "Move-out acknowledgment"}]

    monkeypatch.setattr(outlook_reader, "read_inbox", fake_read_inbox)

    messages = asyncio.run(
        outlook_reader.read_sent_items(
            limit=25,
            timeout_ms=5_000,
            allow_login=False,
        )
    )

    assert calls == {
        "limit": 25,
        "timeout_ms": 5_000,
        "allow_login": False,
        "folder": "sentitems",
    }
    assert messages == [{"subject": "Move-out acknowledgment"}]


def test_read_inbox_rejects_unsupported_folder() -> None:
    with pytest.raises(ValueError, match="Unsupported Outlook folder"):
        asyncio.run(outlook_reader.read_inbox(folder="archive"))


def test_inbox_navigation_uses_configured_outlook_host() -> None:
    assert outlook_reader.INBOX_URL == f"https://{outlook_reader.OUTLOOK_HOST}/mail/"


def test_message_link_uses_selected_folder() -> None:
    inbox_message = outlook_reader._parse_message_snapshot(
        {"conId": "conversation-1"},
    )
    sent_message = outlook_reader._parse_message_snapshot(
        {"conId": "conversation-1"},
        folder="sentitems",
    )

    assert inbox_message["webLink"] == (
        f"https://{outlook_reader.OUTLOOK_HOST}/mail/inbox/id/conversation-1"
    )
    assert sent_message["webLink"] == (
        f"https://{outlook_reader.OUTLOOK_HOST}/mail/sentitems/id/conversation-1"
    )
