import json
from io import BytesIO
from unittest.mock import patch

import pytest

from app.logic.parser import parse_move_out_email


@pytest.fixture(autouse=True)
def disable_gemini_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)


def test_parses_move_out_notice_preview() -> None:
    email = {
        "id": "AQAAA6K8tbsBAAAF1bjQOwAAAAA=",
        "subject": "Move-Out Notice - Flat 302, Prestige Park Sat",
        "from": {
            "emailAddress": {
                "name": "Prashanth Gowda RS",
                "address": "outlook_93B0F56C19D6711F@outlook.com",
            }
        },
        "bodyPreview": (
            "Hi Team, I am writing to formally submit my move-out notice for "
            "Flat 302 at Prestige Park. My lease agreement ends on "
            "November 30, 2026..."
        ),
    }

    assert parse_move_out_email(email) == {
        "graphMessageId": "AQAAA6K8tbsBAAAF1bjQOwAAAAA=",
        "society": "Prestige Park",
        "flat": "302",
        "moveOutDate": "2026-11-30",
        "moveOutDateSource": "body",
        "moveOutDateVague": False,
        "needsReview": False,
        "reviewReason": None,
        "tenantName": "Prashanth Gowda RS",
        "tenantEmail": "outlook_93B0F56C19D6711F@outlook.com",
        "parseStatus": "success",
        "parseMethod": "regex",
        "rawSubject": "Move-Out Notice - Flat 302, Prestige Park Sat",
        "rawPreview": (
            "Hi Team, I am writing to formally submit my move-out notice for "
            "Flat 302 at Prestige Park. My lease agreement ends on "
            "November 30, 2026..."
        ),
    }


def test_ignores_noise_email() -> None:
    email = {
        "subject": "Deployment status SUCCESS",
        "bodyPreview": "Deployment stat Deployment status SUCCESS",
    }

    assert parse_move_out_email(email) is None


def test_returns_missing_data_for_recognized_notice() -> None:
    email = {
        "subject": "Move out notice",
        "bodyPreview": "I plan to vacate soon.",
    }

    result = parse_move_out_email(email)

    assert result is not None
    assert result["parseStatus"] == "missing_data"
    assert result["flat"] is None
    assert result["society"] is None
    assert result["moveOutDate"] is None
    assert result["moveOutDateSource"] == "null"
    assert result["moveOutDateVague"] is True
    assert result["needsReview"] is True


def test_parses_flat_and_society_without_at_preposition() -> None:
    email = {
        "subject": "Notice to vacate Flat 302, Prestige Park",
        "bodyPreview": "My agreement ends on November 30, 2026.",
    }

    result = parse_move_out_email(email)

    assert result is not None
    assert result["flat"] == "302"
    assert result["society"] == "Prestige Park"
    assert result["moveOutDate"] == "2026-11-30"


def test_uses_gemini_fields_when_api_key_is_configured(monkeypatch) -> None:
    email = {
        "id": "ai-message",
        "subject": "I will vacate my apartment",
        "bodyPreview": "I will leave at the end of the month.",
        "from": {
            "emailAddress": {
                "name": "Sender Name",
                "address": "sender@example.com",
            }
        },
    }
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with patch(
        "app.logic.parser._classify_with_gemini",
        return_value={
            "isMoveOutNotice": True,
            "society": "Example Heights",
            "flat": "A-12",
            "moveOutDate": "December 1, 2026",
            "tenantName": "Tenant Name",
        },
    ) as classify:
        result = parse_move_out_email(email)

    classify.assert_called_once()
    assert classify.call_args.kwargs["api_key"] == "test-key"
    assert result is not None
    assert result["society"] == "Example Heights"
    assert result["flat"] == "A-12"
    assert result["tenantName"] == "Tenant Name"
    assert result["parseStatus"] == "success"
    assert result["parseMethod"] == "gemini"


def test_regex_fills_missing_fields_from_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    email = {
        "subject": "Move-Out Notice - Flat 302, Prestige Park",
        "bodyPreview": (
            "My lease agreement ends on November 30, 2026."
        ),
    }
    with patch(
        "app.logic.parser._classify_with_gemini",
        return_value={
            "isMoveOutNotice": True,
            "society": "Prestige Park",
            "flat": None,
            "moveOutDate": None,
            "tenantName": None,
        },
    ):
        result = parse_move_out_email(email)

    assert result is not None
    assert result["flat"] == "302"
    assert result["moveOutDate"] == "2026-11-30"
    assert result["parseMethod"] == "gemini+regex"


def test_gemini_can_classify_keyword_match_as_noise(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with patch(
        "app.logic.parser._classify_with_gemini",
        return_value={
            "isMoveOutNotice": False,
            "society": None,
            "flat": None,
            "moveOutDate": None,
            "tenantName": None,
        },
    ):
        assert parse_move_out_email(
            {"subject": "Move-out policy update", "bodyPreview": "New policy"}
        ) is None


def test_gemini_failure_falls_back_to_regex(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    email = {
        "subject": "Move-Out Notice - Flat 302, Prestige Park",
        "bodyPreview": (
            "Move-out for Flat 302 at Prestige Park. Lease ends on "
            "November 30, 2026."
        ),
    }
    with (
        patch(
            "app.logic.parser._classify_with_gemini",
            side_effect=RuntimeError("test failure"),
        ),
        patch("app.logic.parser.logger.warning") as warning,
    ):
        # A non-API exception is not swallowed; only expected classification
        # errors trigger regex fallback.
        try:
            parse_move_out_email(email)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Unexpected exception was not propagated")
        warning.assert_not_called()

    from app.logic.parser import GeminiClassificationError

    with (
        patch(
            "app.logic.parser._classify_with_gemini",
            side_effect=GeminiClassificationError("simulated API failure"),
        ),
        patch("app.logic.parser.logger.warning") as warning,
    ):
        result = parse_move_out_email(email)

    warning.assert_called_once()
    assert result is not None
    assert result["society"] == "Prestige Park"
    assert result["flat"] == "302"
    assert result["moveOutDate"] == "2026-11-30"
    assert result["parseMethod"] == "regex_fallback"


@pytest.mark.parametrize(
    ("raw_date", "expected"),
    [
        ("2026-09-30", "2026-09-30"),
        ("30/09/2026", "2026-09-30"),
        ("30-09-2026", "2026-09-30"),
        ("September 30, 2026", "2026-09-30"),
        ("30th September, 2026", "2026-09-30"),
        ("Sept 30 2026", "2026-09-30"),
    ],
)
def test_normalizes_supported_date_formats(raw_date, expected):
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat 302, Prestige Park",
            "bodyPreview": f"I plan to move out on {raw_date}.",
        }
    )

    assert result is not None
    assert result["moveOutDate"] == expected
    assert result["moveOutDateSource"] == "body"


def test_prefers_body_date_over_subject_date():
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat 302, Prestige Park on Dec 1, 2026",
            "bodyPreview": (
                "Move-out for Flat 302 at Prestige Park is November 30, 2026."
            ),
        }
    )

    assert result is not None
    assert result["moveOutDate"] == "2026-11-30"
    assert result["moveOutDateSource"] == "body"


def test_uses_subject_date_when_body_has_no_date():
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat 302, Prestige Park - Nov 30 2026",
            "bodyPreview": "I plan to vacate soon.",
        }
    )

    assert result is not None
    assert result["moveOutDate"] == "2026-11-30"
    assert result["moveOutDateSource"] == "subject"
    assert result["moveOutDateVague"] is False


def test_uses_received_date_as_reviewable_fallback():
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat 302, Prestige Park",
            "bodyPreview": "I plan to move out soon.",
            "receivedDateTime": "Tue 10-11-2026 17:06",
        }
    )

    assert result is not None
    assert result["moveOutDate"] == "2026-11-10"
    assert result["moveOutDateSource"] == "received_date"
    assert result["moveOutDateVague"] is True
    assert result["needsReview"] is False
    assert result["parseStatus"] == "success"


def test_flags_move_out_date_before_received_date():
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat 302, Prestige Park",
            "bodyPreview": "I am moving out on Nov 10, 2026.",
            "receivedDateTime": "2026-11-30",
        }
    )

    assert result is not None
    assert result["moveOutDate"] == "2026-11-10"
    assert result["moveOutDateSource"] == "body"
    assert result["moveOutDateVague"] is True
    assert result["needsReview"] is True
    assert result["reviewReason"] == (
        "moveOutDate (2026-11-10) is before email received date "
        "(2026-11-30) — possible invalid notice"
    )


def test_extracts_prefixed_flat_identifier():
    result = parse_move_out_email(
        {
            "subject": "Move-Out Notice - Flat T-17, Krishna Brookfield",
            "bodyPreview": "I am moving out soon.",
        }
    )

    assert result is not None
    assert result["flat"] == "T-17"
    assert result["society"] == "Krishna Brookfield"


def test_pack_builder_accepts_normalized_iso_move_out_date():
    from app.logic.engine import generate_move_out_pack

    pack = generate_move_out_pack(
        {
            "society": "Prestige Park",
            "flat": "302",
            "moveOutDate": "2026-11-30",
            "thm_home_id": "home-1",
        }
    )

    assert pack["reminders"][1]["startISO"].startswith("2026-11-30")


def test_gemini_request_uses_api_key_header_and_json_response(monkeypatch) -> None:
    from app.logic.parser import _classify_with_gemini

    api_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "isMoveOutNotice": True,
                                    "society": "Prestige Park",
                                    "flat": 302,
                                    "moveOutDate": "November 30, 2026",
                                    "tenantName": None,
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }
    response = BytesIO(json.dumps(api_response).encode("utf-8"))
    response.__enter__ = lambda: response
    response.__exit__ = lambda *_args: None
    monkeypatch.setattr("app.logic.parser.urlopen", lambda *_args, **_kwargs: response)

    result = _classify_with_gemini(
        "Move-out for Flat 302 at Prestige Park",
        api_key="test-key",
        model="gemini-test",
    )

    assert result["flat"] == "302"
    assert result["isMoveOutNotice"] is True
