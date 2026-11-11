import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser import outlook_reader


def test_wait_for_inbox_recovers_from_mid_read_auth_redirect(monkeypatch) -> None:
    class Rows:
        def __init__(self, page):
            self.page = page
            self.first = self
            self.waits = 0

        async def wait_for(self, *, state: str, timeout: int) -> None:
            assert state == "visible"
            self.waits += 1
            if self.waits == 1:
                self.page.url = (
                    "https://login.microsoftonline.com/common/oauth2/authorize"
                )
                raise PlaywrightTimeoutError("redirected to login")

        async def count(self) -> int:
            return 1

    class Main:
        async def inner_text(self, *, timeout: int) -> str:
            return ""

    class Page:
        url = outlook_reader.INBOX_URL

        def __init__(self):
            self.rows = Rows(self)

        def locator(self, selector: str):
            if selector == outlook_reader.MESSAGE_ROW_SELECTOR:
                return self.rows
            return type("MainLocator", (), {"first": Main()})()

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            self.url = outlook_reader.INBOX_URL

    prompts = []

    async def fake_to_thread(function, prompt):
        assert function is input
        prompts.append(prompt)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(outlook_reader.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(outlook_reader.asyncio, "sleep", fake_sleep)
    page = Page()

    rows = asyncio.run(outlook_reader._wait_for_inbox_ready(page, 5_000))

    assert rows is page.rows
    assert page.rows.waits == 2
    assert prompts == [
        ">>> Press Enter when the Inbox rows are visible: "
    ]


def test_login_recovery_waits_for_outlook_redirect_to_finish(monkeypatch) -> None:
    class Page:
        url = "https://login.microsoftonline.com/common/oauth2/authorize"

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"

    page = Page()
    prompts = []
    sleeps = []

    async def fake_to_thread(function, prompt):
        assert function is input
        prompts.append(prompt)

    async def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 2:
            page.url = outlook_reader.INBOX_URL

    monkeypatch.setattr(outlook_reader.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(outlook_reader.asyncio, "sleep", fake_sleep)

    asyncio.run(
        outlook_reader._ensure_logged_in(
            page,
            5_000,
            prompt="[outlook] Sign-in needed.",
            folder_url=outlook_reader.INBOX_URL,
        )
    )

    assert len(prompts) == 1
    assert len(sleeps) == 2
