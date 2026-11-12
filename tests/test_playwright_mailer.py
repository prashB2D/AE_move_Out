import asyncio
import sys

import pytest

from app.adapters import playwright_mailer
from app.adapters.notifier import Followup, build_email_followups


@pytest.fixture(autouse=True)
def _no_settle_waits(monkeypatch):
    monkeypatch.setattr(playwright_mailer, "POST_PASTE_SETTLE_S", 0)
    monkeypatch.setattr(playwright_mailer, "POST_SEND_SETTLE_S", 0)


class RecordingNotifier:
    def __init__(self) -> None:
        self.followups = []

    async def notify(self, followup) -> None:
        self.followups.append(followup)


def test_send_pack_emails_redirects_all_messages_to_fixed_test_inbox(
    monkeypatch, capsys
) -> None:
    sent_messages = []

    async def fake_send_one(**kwargs) -> None:
        sent_messages.append(kwargs)

    monkeypatch.setattr(playwright_mailer, "send_one", fake_send_one)
    pack = {
        "emailsToSend": [
            {
                "recipient": "tenant@example.com",
                "cc": "owner@example.com",
                "immediate": True,
                "subject": "Tenant notice",
                "html": "<p>Tenant message</p>",
            },
            {
                "recipient": "owner@example.com",
                "immediate": True,
                "subject": "Owner notice",
                "body": "<p>Owner message</p>",
            },
        ]
    }

    result = asyncio.run(
        playwright_mailer.send_pack_emails(
            pack,
            test_email="KINGPRASHU123@gmail.com",
            notifier=RecordingNotifier(),
        )
    )

    assert result == {"sent": 2, "failed": 0}
    assert sent_messages == [
        {
            "to": playwright_mailer.TEST_EMAIL,
            "cc": "",
            "subject": "Tenant notice",
            "html_body": "<p>Tenant message</p>",
        },
        {
            "to": playwright_mailer.TEST_EMAIL,
            "cc": "",
            "subject": "Owner notice",
            "html_body": "<p>Owner message</p>",
        },
    ]
    assert "sending 2, skipping 0 scheduled" in capsys.readouterr().out


@pytest.mark.parametrize("test_email", [None, "", "someone@example.com"])
def test_send_pack_emails_refuses_missing_or_non_test_redirect(
    test_email,
) -> None:
    with pytest.raises(RuntimeError, match="test-email"):
        asyncio.run(
            playwright_mailer.send_pack_emails(
                {"emailsToSend": []}, test_email=test_email
            )
        )


def test_send_one_refuses_non_test_recipient_before_browser_launch() -> None:
    with pytest.raises(RuntimeError, match="must be"):
        asyncio.run(
            playwright_mailer.send_one(
                to="tenant@example.com",
                cc="",
                subject="Notice",
                html_body="<p>Notice</p>",
            )
        )


@pytest.mark.parametrize(
    "data",
    [
        {"emailsToSend": []},
        {"pack": {"emailsToSend": []}},
        {"auto_processed": [{"pack": {"emailsToSend": []}}]},
        [{"pack": {"emailsToSend": []}}],
    ],
)
def test_extract_pack_accepts_documented_shapes(data) -> None:
    assert playwright_mailer._extract_pack(data) == {"emailsToSend": []}


def test_extract_pack_rejects_malformed_emails() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        playwright_mailer._extract_pack({"emailsToSend": {}})
    with pytest.raises(ValueError, match="must be an object"):
        playwright_mailer._extract_pack({"emailsToSend": ["not an email object"]})


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.filled = []
        self.clicked = 0
        self.waited = []

    @property
    def first(self):
        return self

    def filter(self, visible: bool = False):
        return self

    async def count(self) -> int:
        return 1 if self.selector in self.page.visible else 0

    async def click(self, **kwargs) -> None:
        self.clicked += 1
        self.page.events.append(("click", self.selector))

    async def fill(self, value: str, **kwargs) -> None:
        self.filled.append(value)

    async def wait_for(self, state: str, timeout: int) -> None:
        self.waited.append(state)

    async def get_attribute(self, name: str):
        return self.page.attrs.get((self.selector, name))

    async def focus(self) -> None:
        self.page.events.append(("focus", self.selector))

    async def evaluate(self, script: str, arg=None):
        return True


class FakeKeyboard:
    def __init__(self, page) -> None:
        self.page = page
        self.pressed = []
        self.typed = []

    async def press(self, value: str) -> None:
        self.pressed.append(value)

    async def type(self, value: str, delay: int = 0) -> None:
        self.typed.append(value)
        self.page.events.append(("type", value))


ACTUAL_OUTLOOK = {
    'div[contenteditable="true"][aria-label="To"]',
    'input[aria-label="Subject"]',
    'div[role="textbox"][aria-label="Message body"]',
    'button[aria-label="Send"]',
}


class FakePage:
    def __init__(self, visible=ACTUAL_OUTLOOK) -> None:
        self.visible = set(visible)
        self.locators = {}
        self.events = []
        self.keyboard = FakeKeyboard(self)
        self.evaluated = None
        self.attrs = {}

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator(self, selector))

    async def evaluate(self, script: str, value: str) -> None:
        self.evaluated = (script, value)

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def test_fill_compose_preserves_html_and_types_test_recipient() -> None:
    page = FakePage()
    body = "<h1>Move-out</h1><p>Formatted content</p>"

    asyncio.run(
        playwright_mailer._fill_compose(
            page,
            to=playwright_mailer.TEST_EMAIL,
            cc="",
            subject="Move-out notice",
            html_body=body,
        )
    )

    to_events = page.events[:2]
    assert to_events == [
        ("click", 'div[contenteditable="true"][aria-label="To"]'),
        ("type", playwright_mailer.TEST_EMAIL),
    ]
    subject = page.locators['input[aria-label="Subject"]']
    assert subject.filled == ["Move-out notice"]
    assert ("focus", 'div[role="textbox"][aria-label="Message body"]') in page.events
    # Subject is filled, never clicked (suggestion dropdown can cover it).
    assert subject.clicked == 0
    assert page.evaluated[1] == body
    assert page.keyboard.pressed == [
        "Enter",
        "Meta+V" if sys.platform == "darwin" else "Control+V",
    ]


def test_fill_compose_falls_back_to_alternate_selectors() -> None:
    page = FakePage(
        visible={
            'input[aria-label="To"]',
            'input[placeholder*="subject" i]',
            '[contenteditable="true"][aria-label*="body" i]',
            'button[title^="Send" i]',
        }
    )

    asyncio.run(
        playwright_mailer._fill_compose(
            page,
            to=playwright_mailer.TEST_EMAIL,
            cc="",
            subject="Notice",
            html_body="<p>x</p>",
        )
    )

    assert page.locators['input[aria-label="To"]'].clicked == 1
    assert page.locators['input[placeholder*="subject" i]'].filled == ["Notice"]


def test_to_selectors_never_use_substring_match_on_to() -> None:
    # Substring matches like aria-label*="To" hit hidden unrelated inputs.
    for selector in playwright_mailer.TO_SELECTORS:
        assert '*="To"' not in selector and '*="To" i' not in selector


def test_fill_compose_reports_missing_to_field(monkeypatch) -> None:
    page = FakePage(visible=set())
    with pytest.raises(RuntimeError, match="To field.*--dump-compose-html"):
        asyncio.run(
            playwright_mailer._find_visible(
                page, playwright_mailer.TO_SELECTORS, 0, "To field"
            )
        )


def test_send_current_clicks_send_once_and_waits_for_close() -> None:
    page = FakePage()
    asyncio.run(playwright_mailer._send_current(page))

    assert page.locators['button[aria-label="Send"]'].clicked == 1
    body = page.locators['div[role="textbox"][aria-label="Message body"]']
    assert body.waited == ["hidden"]


def test_send_pack_emails_respects_limit(monkeypatch) -> None:
    sent = []

    async def fake_send_one(**kwargs) -> None:
        sent.append(kwargs["subject"])

    monkeypatch.setattr(playwright_mailer, "send_one", fake_send_one)
    pack = {
        "emailsToSend": [
            {"subject": f"S{i}", "html": "<p>x</p>", "immediate": True} for i in range(5)
        ]
    }
    result = asyncio.run(
        playwright_mailer.send_pack_emails(
            pack, test_email=playwright_mailer.TEST_EMAIL, limit=1
        )
    )
    assert result == {"sent": 1, "failed": 0}
    assert sent == ["S0"]

    with pytest.raises(ValueError, match="limit"):
        asyncio.run(
            playwright_mailer.send_pack_emails(
                pack, test_email=playwright_mailer.TEST_EMAIL, limit=0
            )
        )


def test_fill_compose_tabs_then_refuses_if_recipient_not_committed(
    monkeypatch,
) -> None:
    page = FakePage()
    page.attrs[('button[aria-label="Send"]', "aria-disabled")] = "true"

    async def no_wait(page, timeout_ms):
        return await real_wait(page, 0)

    real_wait = playwright_mailer._wait_send_enabled
    monkeypatch.setattr(playwright_mailer, "_wait_send_enabled", no_wait)
    with pytest.raises(RuntimeError, match="not committed"):
        asyncio.run(
            playwright_mailer._fill_compose(
                page,
                to=playwright_mailer.TEST_EMAIL,
                cc="",
                subject="Notice",
                html_body="<p>x</p>",
            )
        )
    assert page.keyboard.pressed == ["Enter", "Tab"]


def test_send_current_refuses_when_send_stays_disabled(monkeypatch) -> None:
    page = FakePage()
    page.attrs[('button[aria-label="Send"]', "aria-disabled")] = "true"

    async def never(page, timeout_ms):
        return False

    monkeypatch.setattr(playwright_mailer, "_wait_send_enabled", never)
    with pytest.raises(RuntimeError, match="stayed disabled"):
        asyncio.run(playwright_mailer._send_current(page))
    assert all(loc.clicked == 0 for loc in page.locators.values())


def test_fill_compose_refuses_cc_recipients() -> None:
    with pytest.raises(RuntimeError, match="CC recipients are disabled"):
        asyncio.run(
            playwright_mailer._fill_compose(
                FakePage(),
                to=playwright_mailer.TEST_EMAIL,
                cc="owner@example.com",
                subject="Notice",
                html_body="<p>Notice</p>",
            )
        )


def test_send_pack_emails_sends_only_immediate_by_default(monkeypatch) -> None:
    sent = []

    async def fake_send_one(**kwargs) -> None:
        sent.append(kwargs["subject"])

    monkeypatch.setattr(playwright_mailer, "send_one", fake_send_one)
    pack = {
        "emailsToSend": [
            {"subject": "Now", "html": "<p>x</p>", "immediate": True},
            {"subject": "Later", "html": "<p>x</p>", "immediate": False},
            {"subject": "Unflagged", "html": "<p>x</p>"},
            {"subject": "Truthy string", "html": "<p>x</p>", "immediate": "true"},
        ]
    }
    notifier = RecordingNotifier()
    result = asyncio.run(
        playwright_mailer.send_pack_emails(
            pack, test_email=playwright_mailer.TEST_EMAIL, notifier=notifier
        )
    )
    assert result == {"sent": 1, "failed": 0}
    assert sent == ["Now"]

    sent.clear()
    asyncio.run(
        playwright_mailer.send_pack_emails(
            pack,
            test_email=playwright_mailer.TEST_EMAIL,
            include_scheduled=True,
            notifier=notifier,
        )
    )
    assert sent == ["Now", "Later", "Unflagged", "Truthy string"]


def test_notifier_gets_followup_per_role_only_after_successful_send(
    monkeypatch,
) -> None:
    async def fake_send_one(**kwargs) -> None:
        if kwargs["subject"] == "Broken":
            raise RuntimeError("boom")

    monkeypatch.setattr(playwright_mailer, "send_one", fake_send_one)
    pack = {
        "ownerPhone": "911111111111",
        "tenantPhone": "922222222222",
        "emailsToSend": [
            {"subject": "Owner", "html": "x", "immediate": True, "role": "owner"},
            {"subject": "Broken", "html": "x", "immediate": True, "role": "tenant"},
            {
                "subject": "Both",
                "html": "x",
                "immediate": True,
                "role": "owner and tenant",
            },
        ],
    }
    notifier = RecordingNotifier()
    result = asyncio.run(
        playwright_mailer.send_pack_emails(
            pack, test_email=playwright_mailer.TEST_EMAIL, notifier=notifier
        )
    )
    assert result == {"sent": 2, "failed": 1}
    assert [(f.email_subject, f.role, f.phone) for f in notifier.followups] == [
        ("Owner", "owner", "911111111111"),
        ("Both", "owner", "911111111111"),
        ("Both", "tenant", "922222222222"),
    ]


def test_build_email_followups_skips_roles_without_phone() -> None:
    followups = build_email_followups(
        {"subject": "S", "role": "tenant"}, {"tenantPhone": ""}
    )
    assert followups == []
