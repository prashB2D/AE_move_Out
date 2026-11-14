import json

import pytest

from app import run_flow


def test_extract_summary_skips_log_lines_with_braces() -> None:
    text = '[dedup] subject="{weird}"\nlog {x}\n' + json.dumps({"auto_processed": [1]}, indent=2)
    assert run_flow.extract_summary(text) == {"auto_processed": [1]}


def test_extract_summary_requires_json() -> None:
    with pytest.raises(ValueError):
        run_flow.extract_summary("only logs")


def test_unknown_skip_step_is_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        run_flow.main(["--skip", "sendd"])
    assert "unknown step" in capsys.readouterr().err


def test_no_pack_stops_after_review(tmp_path, monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(run_flow, "_run", lambda args, browser: calls.append(args) or 0)
    saved = tmp_path / "out.txt"
    saved.write_text(json.dumps({"auto_processed": [], "sent_to_review": []}, indent=2))
    assert run_flow.main(["--from-output", str(saved)]) == 0
    assert calls == []  # nothing sent, drafted or saved
    assert "No property was auto-processed" in capsys.readouterr().out
