from pathlib import Path

from app.browser.outlook_probe import (
    DEFAULT_OUTLOOK_HOST,
    get_outlook_host,
    get_profile_path,
    is_outlook_mail_url,
)


def test_accepts_outlook_mailbox_urls() -> None:
    assert is_outlook_mail_url("https://outlook.office.com/mail/")
    assert is_outlook_mail_url("https://outlook.live.com/mail/inbox/id")


def test_defaults_to_personal_outlook_host(monkeypatch) -> None:
    monkeypatch.delenv("MOVEOUT_OUTLOOK_HOST", raising=False)

    assert get_outlook_host() == DEFAULT_OUTLOOK_HOST == "outlook.live.com"


def test_allows_configured_work_outlook_host(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_OUTLOOK_HOST", "outlook.office.com")

    assert get_outlook_host() == "outlook.office.com"


def test_rejects_unrecognized_outlook_host(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_OUTLOOK_HOST", "attacker.invalid")

    try:
        get_outlook_host()
    except ValueError as error:
        assert "MOVEOUT_OUTLOOK_HOST" in str(error)
    else:
        raise AssertionError("Expected unsupported Outlook host to be rejected.")


def test_rejects_non_outlook_or_non_mail_urls() -> None:
    assert not is_outlook_mail_url("http://outlook.office.com/mail/")
    assert not is_outlook_mail_url("https://outlook.office.com/calendar/")
    assert not is_outlook_mail_url("https://outlook.office.com.attacker.invalid/mail/")
    assert not is_outlook_mail_url("https://example.com/mail/")


def test_default_profile_path_is_anchored_to_project_not_working_directory(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MOVEOUT_BROWSER_PROFILE", raising=False)

    profile_path = get_profile_path()

    assert profile_path == Path(__file__).resolve().parents[1] / "browser_data" / "outlook"


def test_profile_path_environment_override_is_expanded_and_resolved(
    monkeypatch, tmp_path
) -> None:
    custom_path = tmp_path / "custom-profile"
    monkeypatch.setenv("MOVEOUT_BROWSER_PROFILE", str(custom_path))

    assert get_profile_path() == custom_path.resolve()
