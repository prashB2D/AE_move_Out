import pytest

from app.adapters.review_notifier import review_html
from app.logic import setup_reply as sr
from app.main import run_once

REVIEW_ITEM = {
    "society": "Krishna Brookfeld", "flat": "", "moveOutDate": None,
    "tenantName": "Atharva Singh", "tenantEmail": "atharvasingh910@gmail.com",
    "reviewReason": "No move-out date", "thm_status": "not_found",
}


def _staff_reply_html() -> str:
    """Staff edits the block from the real review email; original is quoted."""
    original = review_html(REVIEW_ITEM)
    edited = (original
              .replace("Property Complex Name: Krishna Brookfeld<br>",
                       "Property Complex Name: Krishna Brookfield<br>")
              .replace("Flat Number: <br>", "Flat Number: Flat # T-17<br>")
              .replace("Vacate Date: <br>", "Vacate Date: 15-Dec-2026<br>")
              .replace("Owner Phone: <br>", "Owner Phone: N/A<br>")
              .replace("Property Type (PM or NonPM): <br>",
                       "Property Type (PM or NonPM): PM<br>"))
    return f"<div>Fixed, please run.</div>{edited}<blockquote>{original}</blockquote>"


def _reply(body, subject="Re: [MO-SETUP] Move-out review needed: Krishna Brookfeld ?"):
    return {"id": "r1", "subject": subject,
            "body": {"contentType": "html", "content": body},
            "receivedDateTime": "2026-11-12T10:00:00+05:30",
            "from": {"emailAddress": {"name": "Staff", "address": "kingprashu123@gmail.com"}}}


@pytest.mark.parametrize("subject, expected", [
    ("Re: [MO-SETUP] Move-out review needed: X 1", True),
    ("RE: Re: [mo-setup] x", True),
    ("AW: [MO-SETUP] x", True),
    ("[MO-SETUP] Move-out review needed: X 1", True),  # Outlook drops "Re:"
    ("Re: Move-out notice", False),
    (None, False),
])
def test_is_review_reply(subject, expected) -> None:
    assert sr.is_review_reply(subject) is expected


def test_parses_first_block_from_real_review_email_round_trip() -> None:
    parsed = sr.parse_setup_reply(_reply(_staff_reply_html()))
    assert parsed["manualConfirmed"] is True
    assert parsed["parseStatus"] == "success"
    assert parsed["society"] == "Krishna Brookfield"  # edited, not quoted typo
    assert parsed["flat"] == "T-17"
    assert parsed["moveOutDate"] == "15-Dec-2026"
    assert parsed["tenantEmail"] == "atharvasingh910@gmail.com"
    assert parsed["ownerPhone"] is None  # N/A placeholder dropped
    assert parsed["propertyType"] == "PM"
    assert parsed["staffReplyFrom"] == "kingprashu123@gmail.com"


def test_plain_text_quoted_reply_is_parsed() -> None:
    body = ("ok\n> ----- MOVE-OUT DETAILS (edit values, keep labels) -----\n"
            "> Property Complex Name: Purva Skywood\n> Flat Number: C-1304\n"
            "> Property Type (PM or NonPM): NonPM\n> ----- END MOVE-OUT DETAILS -----\n")
    parsed = sr.parse_setup_reply(_reply(body))
    assert (parsed["society"], parsed["flat"], parsed["propertyType"]) == (
        "Purva Skywood", "C-1304", "NonPM")


def test_blank_society_or_flat_is_incomplete() -> None:
    parsed = sr.parse_setup_reply(_reply(review_html(REVIEW_ITEM)))  # flat left blank
    assert parsed["parseStatus"] == "manual_reply_incomplete"
    assert parsed["manualConfirmed"] is True


def test_reply_without_block_is_not_parsed() -> None:
    assert sr.parse_setup_reply(_reply("thanks"))["parseStatus"] == "manual_reply_received"


def test_subject_without_re_prefix_and_flattened_preview_parse() -> None:
    preview = ("Hi team, - MOVE-OUT DETAILS (edit values, keep labels) - Property Complex "
               "Name: Krishna Brookfield Flat Number: T-17 Vacate Date: 15-Dec-2026 "
               "Tenant Name: Atharva Singh Tenant Email: atharvasingh910@g")
    email = {"id": "x", "subject": "[MO-SETUP] Move-out review needed: Krishna Brookfield T-17",
             "bodyPreview": preview}
    parsed = sr.parse_setup_reply(email)
    assert parsed["parseStatus"] == "success"
    assert (parsed["society"], parsed["flat"], parsed["moveOutDate"]) == (
        "Krishna Brookfield", "T-17", "15-Dec-2026")
    assert parsed["tenantEmail"] is None  # truncated address is not trusted


def _run(emails, thm):
    return run_once(
        emails,
        dedup_check=lambda _e: False,
        property_dedup_check=lambda _s, _f: False,
        property_mark=lambda _s, _f: None,
        sent_items=[],
        message_recorder=lambda *_a: None,
        pack_recorder=lambda *_a: None,
        thm_lookup=thm,
    )


def test_reply_skips_ai_and_runs_engine_even_if_thm_not_found(monkeypatch) -> None:
    import app.main as main_module

    def no_ai(_email):
        raise AssertionError("AI parser must not run for staff replies")

    monkeypatch.setattr(main_module, "parse_move_out_email", no_ai)
    summary = _run([_reply(_staff_reply_html())],
                   lambda *_a: {"thm_home_id": None, "thm_status": "not_found"})
    assert len(summary["auto_processed"]) == 1
    canonical = summary["auto_processed"][0]["canonical"]
    assert canonical["manualConfirmed"] is True
    assert (canonical["society"], canonical["flat"]) == ("Krishna Brookfield", "T-17")
    assert canonical["thm_status"] == "not_found"


def test_staff_values_override_thm_but_thm_fills_gaps() -> None:
    summary = _run([_reply(_staff_reply_html())], lambda *_a: {
        "thm_home_id": "H1", "thm_status": "matched", "thm_category": "PM",
        "society": "Krishna Brookfield", "flat": "T-17",
        "ownerName": "ANIL KRISHNAN", "tenantName": "THM name"})
    canonical = summary["auto_processed"][0]["canonical"]
    assert canonical["thm_home_id"] == "H1"
    assert canonical["ownerName"] == "ANIL KRISHNAN"  # filled by THM
    assert canonical["tenantName"] == "Atharva Singh"  # staff value wins
    assert canonical["moveOutDate"] == "15-Dec-2026"


def test_reply_marked_nonpm_is_skipped() -> None:
    body = _staff_reply_html().replace(
        "Property Type (PM or NonPM): PM<br>", "Property Type (PM or NonPM): NonPM<br>", 1)
    summary = _run([_reply(body)], lambda *_a: {"thm_home_id": None})
    assert summary["auto_processed"] == [] and len(summary["ignored_noise"]) == 1
