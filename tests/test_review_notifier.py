import asyncio
import json

import pytest

from app.adapters import review_notifier as r

ITEM = {
    "society": "Krishna Brookfield", "flat": "T-17", "moveOutDate": None,
    "tenantName": "Atharva Singh", "tenantEmail": "atharvasingh910@gmail.com",
    "parseStatus": "missing_data", "reviewReason": "No move-out date was found",
    "rawSubject": "notice <script>x</script>", "rawPreview": "I am moving & leaving",
    "thm_status": "not_found", "parseMethod": "gemini",
}
NOW = 1_800_000_000_000
DAY = 86_400_000


def test_staff_recipients_split_on_semicolon_or_comma() -> None:
    assert r.staff_recipients("a@x.com; b@y.com") == ["a@x.com", "b@y.com"]
    assert r.staff_recipients("a@x.com,b@y.com") == ["a@x.com", "b@y.com"]
    with pytest.raises(RuntimeError):
        r.staff_recipients("not-an-address")


def test_property_key_normalizes_and_handles_unknown_property() -> None:
    assert r.property_key(ITEM) == "rev:krishnabrookfield|t17"
    assert r.property_key({"society": " ", "flat": None}) is None


def test_dedup_window_and_exemptions() -> None:
    seen = {"rev:krishnabrookfield|t17": NOW - 2 * DAY}
    assert r.dedup_reason(ITEM, seen, NOW) == "reviewed 2 days ago"
    assert r.dedup_reason({**ITEM, "manualConfirmed": True}, seen, NOW) is None
    assert r.dedup_reason({**ITEM, "society": "", "flat": ""}, seen, NOW) is None
    assert r.dedup_reason({**ITEM, "flat": "T-18"}, seen, NOW) is None


def test_load_seen_drops_entries_older_than_window(tmp_path) -> None:
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"rev:old|1": NOW - 31 * DAY, "rev:new|1": NOW - DAY}))
    assert r.load_seen(path, NOW) == {"rev:new|1": NOW - DAY}
    assert r.load_seen(tmp_path / "missing.json", NOW) == {}


def test_review_email_escapes_tenant_text_and_fills_block() -> None:
    body = r.review_html(ITEM)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "I am moving &amp; leaving" in body
    assert "Property Complex Name: Krishna Brookfield<br>" in body
    assert "did not give a firm move-out date" in body
    assert "flagged as move-out notice" in body
    assert r.review_subject(ITEM) == "[MO-SETUP] Move-out review needed: Krishna Brookfield T-17"
    assert r.review_subject({}) == "[MO-SETUP] Move-out review needed: ? ?"


def test_all_deduped_never_opens_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(r, "async_playwright", None)  # browser use would crash
    path = tmp_path / "seen.json"
    import time
    path.write_text(json.dumps({"rev:krishnabrookfield|t17": int(time.time() * 1000)}))
    result = asyncio.run(r.send_reviews([ITEM], seen_path=path))
    assert result == {"sent": 0, "deduped": 1, "failed": 0}


def test_dry_run_reads_pipeline_output(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(r, "SEEN_PATH", tmp_path / "seen.json")
    out_file = tmp_path / "out.json"
    out_file.write_text("log line\n" + json.dumps({"sent_to_review": [ITEM]}))
    asyncio.run(r._main(["--pipeline-output", str(out_file), "--dry-run"]))
    out = capsys.readouterr().out
    assert "Found 1 email(s)" in out
    assert "would send: [MO-SETUP] Move-out review needed: Krishna Brookfield T-17" in out
