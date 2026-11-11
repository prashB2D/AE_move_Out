import asyncio

from app.main import run_once

FAKE_EMAILS = [
    {
        "id": "1",
        "subject": "Deployment status SUCCESS",
        "bodyPreview": "ok",
        "receivedDateTime": "2026-11-10T09:49:00+05:30",
        "from": {"emailAddress": {"name": "Sys", "address": "s@example.com"}},
    },
    {
        "id": "2",
        "subject": "Hi",
        "bodyPreview": "test",
        "receivedDateTime": "2026-11-10T09:48:00+05:30",
        "from": {"emailAddress": {"name": "P", "address": "p@example.com"}},
    },
    {
        "id": "3",
        "subject": "Move-Out Notice - Flat 302, Prestige Park",
        "bodyPreview": (
            "Move-out for Flat 302 at Prestige Park. Lease ends on "
            "November 30, 2026."
        ),
        "receivedDateTime": "2026-11-10T09:49:00+05:30",
        "from": {"emailAddress": {"name": "Tenant", "address": "t@example.com"}},
    },
    {
        "id": "4",
        "subject": "Move-Out Notice - Flat 999, Unknown Towers",
        "bodyPreview": "Move-out for Flat 999 at Unknown Towers. Lease ends Dec 1 2026.",
        "receivedDateTime": "2026-11-10T09:49:00+05:30",
        "from": {"emailAddress": {"name": "Tenant2", "address": "t2@example.com"}},
    },
    {
        "id": "5",
        "subject": "Re: [MO-SETUP] Move-Out review",
        "bodyPreview": "MOVE-OUT DETAILS...",
        "receivedDateTime": "2026-11-10T09:49:00+05:30",
        "from": {"emailAddress": {"name": "Staff", "address": "s@example.com"}},
    },
]


def test_end_to_end_flow_routes_mock_emails() -> None:
    summary = run_once(
        FAKE_EMAILS,
        dedup_check=lambda _email: False,
        property_dedup_check=lambda _society, _flat: False,
        property_mark=lambda _society, _flat: None,
        message_recorder=lambda _email, _bucket, _canonical: None,
        pack_recorder=lambda _canonical, _pack: None,
        thm_lookup=lambda _society, flat, _email: (
            {
                "thm_home_id": "THM-HOME-302",
                "thm_project_id": "THM-PROJ-PP",
                "thm_status": "matched",
            }
            if flat == "302"
            else {"thm_home_id": None, "thm_status": "not_found"}
        ),
    )

    assert len(summary["auto_processed"]) == 1
    assert summary["auto_processed"][0]["canonical"]["flat"] == "302"
    assert summary["auto_processed"][0]["pack"]["checklist"]["tab_name"] == (
        "Prestige Park 302"
    )
    assert summary["auto_processed"][0]["execution"] == {
        "mode": "dry_run",
        "tickets_planned": 11,
        "tickets_skipped": 0,
        "emails_planned": 1,
        "whatsapp_planned": 0,
        "whatsapp_skipped": 0,
        "reminders_planned": 2,
        "reminders_skipped": 0,
    }
    assert len(summary["sent_to_review"]) == 1
    assert summary["sent_to_review"][0]["society"] == "Unknown Towers"
    assert len(summary["setup_replies"]) == 1
    assert len(summary["ignored_noise"]) == 2
    assert summary["dropped_duplicate_or_stale"] == []


def test_end_to_end_flow_drops_duplicate() -> None:
    summary = run_once(
        [FAKE_EMAILS[0]],
        dedup_check=lambda _email: True,
        message_recorder=lambda _email, _bucket, _canonical: None,
    )

    assert summary["dropped_duplicate_or_stale"] == ["1"]
    assert summary["ignored_noise"] == []


def test_thm_lookup_only_parses_and_fetches_without_building_pack(monkeypatch) -> None:
    from app import main

    parsed = {
        "graphMessageId": "move-out-1",
        "society": "Example Heights",
        "flat": "A-12",
        "moveOutDate": "December 1, 2026",
        "tenantName": "Tenant Name",
        "tenantEmail": "tenant@example.com",
        "parseStatus": "success",
    }
    thm_calls = []
    monkeypatch.setattr(main, "parse_move_out_email", lambda _email: parsed)

    def fake_thm_lookup(society, flat, tenant_email):
        thm_calls.append((society, flat, tenant_email))
        return {
            "thm_home_id": "real-home-id",
            "thm_project_id": "real-project-id",
            "ownerEmail": "owner@example.com",
            "thm_status": "matched",
        }

    result = main.run_thm_lookup_only(
        [{"id": "move-out-1"}],
        thm_lookup=fake_thm_lookup,
    )

    assert thm_calls == [
        ("Example Heights", "A-12", "tenant@example.com")
    ]
    assert result == {
        "thm_matches": [
            {
                **parsed,
                "thm_home_id": "real-home-id",
                "thm_project_id": "real-project-id",
                "ownerEmail": "owner@example.com",
                "thm_status": "matched",
            }
        ],
        "needs_review": [],
        "ignored_noise": [],
    }


def test_fresh_run_uses_ephemeral_dedup_and_logs_each_decision(monkeypatch, capsys):
    from app import main

    monkeypatch.setenv(
        "MOVEOUT_DEDUP_DB",
        "postgresql://invalid:invalid@127.0.0.1:1/should-not-connect",
    )
    monkeypatch.setenv(
        "MOVEOUT_PROPERTY_DEDUP_DB",
        "postgresql://invalid:invalid@127.0.0.1:1/should-not-connect",
    )
    monkeypatch.setenv("MOVEOUT_TEST_EMAIL", "")
    message = {
        "id": "fresh-message-001",
        "subject": "Move-Out Notice - Flat 302, Prestige Park",
        "bodyPreview": (
            "Move-out for Flat 302 at Prestige Park. Lease ends on "
            "November 30, 2026."
        ),
        "receivedDateTime": "2026-11-10T09:49:00+05:30",
        "from": {
            "emailAddress": {
                "name": "Tenant",
                "address": "tenant@example.com",
            }
        },
    }
    messages = [message, message]

    result = main._run_once_fresh(
        messages,
        thm_lookup=lambda *_args: {
            "thm_home_id": "home-real",
            "thm_project_id": "project-real",
            "thm_status": "matched",
        },
    )

    assert len(result["auto_processed"]) == 1
    assert result["dropped_duplicate_or_stale"] == ["fresh-message-001"]
    output = capsys.readouterr().out
    assert (
        '[dedup] fresh-messag subject="Move-Out Notice - Flat 302, Prestige Park" '
        "-> auto_processed"
    ) in output
    assert (
        '[dedup] fresh-messag subject="Move-Out Notice - Flat 302, Prestige Park" '
        "-> dropped_duplicate_or_stale"
    ) in output


def test_read_only_rows_print_compact_fields(capsys):
    from app.main import _print_read_only_rows

    _print_read_only_rows(
        [
            {
                "id": "row-id",
                "conversationId": "conversation-id",
                "from": {
                    "emailAddress": {
                        "name": "Tenant Name",
                        "address": "tenant@example.com",
                    }
                },
                "subject": "Move-out notice",
                "receivedDateTime": "Today 10:30",
                "bodyPreview": "Short preview",
            }
        ]
    )

    assert capsys.readouterr().out.strip() == (
        '[read-only] row 1: id=row-id conId=conversation-id '
        'sender="Tenant Name" subject="Move-out notice" '
        'received="Today 10:30" preview="Short preview"'
    )


def test_read_only_cli_exits_before_parser_or_thm(monkeypatch, capsys):
    from app import main

    async def fake_read_live_emails(_limit):
        return [
            {
                "id": "row-id",
                "conversationId": "conversation-id",
                "subject": "Move-out notice",
                "bodyPreview": "Short preview",
                "from": {"emailAddress": {"name": "Tenant"}},
            }
        ]

    monkeypatch.setattr(main, "_read_live_emails", fake_read_live_emails)
    monkeypatch.setattr(
        main,
        "parse_move_out_email",
        lambda _email: (_ for _ in ()).throw(AssertionError("parser was called")),
    )

    result = asyncio.run(
        main._run_cli(
            None,
            input_live=True,
            limit=5,
            read_only=True,
        )
    )

    assert len(result["read_only"]) == 1
    output = capsys.readouterr().out
    assert '[read-only] row 1: id=row-id conId=conversation-id' in output
    assert "parser was called" not in output


def test_live_cli_reads_inbox_with_configured_limit(monkeypatch, capsys) -> None:
    from app import main
    from app.browser import outlook_reader

    monkeypatch.setenv("MOVEOUT_TEST_EMAIL", "qa@example.com")
    monkeypatch.setenv("MOVEOUT_DRY_RUN", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_TICKETS", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_WHATSAPP", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_CALENDAR", "true")
    calls: dict[str, int] = {}

    async def fake_read_inbox(*, limit: int, timeout_ms: int):
        calls["limit"] = limit
        calls["timeout_ms"] = timeout_ms
        return []

    monkeypatch.setattr(outlook_reader, "read_inbox", fake_read_inbox)

    summary = asyncio.run(
        main._run_cli(None, input_live=True, limit=7)
    )

    output = capsys.readouterr().out
    assert calls == {"limit": 7, "timeout_ms": 60_000}
    assert summary["auto_processed"] == []
    assert "Reading Live Emails from Outlook" in output
    assert "Read 0 emails." in output
    assert "MOVEOUT TEST RUN — channel status" in output
    assert "Email       : DRY RUN (test recipient: qa@example.com)" in output
    assert "THM tickets : DISABLED" in output
    assert "WhatsApp    : DISABLED" in output
    assert "Calendar    : DISABLED" in output


def test_thm_lookup_only_cli_banner_states_no_actions(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    from app import main

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "run_thm_lookup_only", lambda _emails: {
        "thm_matches": [],
        "needs_review": [],
        "ignored_noise": [],
    })
    input_path = tmp_path / "emails.json"
    input_path.write_text("[]", encoding="utf-8")
    asyncio.run(
        main._run_cli(
            input_path,
            input_live=False,
            limit=1,
            thm_lookup_only=True,
        )
    )

    output = capsys.readouterr().out
    assert "MOVEOUT THM LOOKUP-ONLY RUN" in output
    assert "regex only (GEMINI_API_KEY not configured)" in output
    assert "Tickets     : NOT RUN" in output
    assert "Email       : NOT RUN" in output
    assert "WhatsApp    : NOT RUN" in output
    assert "Calendar    : NOT RUN" in output
    assert "Dedup writes: NOT RUN" in output
