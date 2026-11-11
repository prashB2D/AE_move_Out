# Move-Out Automation (Python)

This repository contains the Outlook Web feasibility probe and the read-only
inbox reader. It is not the complete n8n migration.

## Run the read-only Outlook probe

1. Install Python dependencies:

   ```sh
   python3 -m pip install -r requirements.txt
   python3 -m playwright install chromium
   ```

2. Start the probe (defaults to the personal Outlook host `outlook.live.com`):

   ```sh
   python3 -m app.browser.outlook_probe
   ```

3. Sign in to the authorized Outlook account in the opened browser, complete any
   MFA challenge yourself, wait for the Inbox and its message rows to appear,
   then press Enter. The probe saves the session cookies before it exits.

The browser profile is stored in `browser_data/outlook/` by default so sign-in
can persist between runs. Set `MOVEOUT_BROWSER_PROFILE` to use another local
directory, preferably an absolute path. Treat this profile as sensitive
authentication data: do not copy it, share it, or add it to version control.
After a successful probe, session cookies are also stored in
`browser_data/outlook/auth.json` with owner-only file permissions. This file is
ignored by Git. It is a bearer credential: anyone who can read it may be able
to use the signed-in session. Do not upload, email, or share it.

The probe and reader default to `outlook.live.com` for personal Outlook
accounts. For a work or school account, set `MOVEOUT_OUTLOOK_HOST=outlook.office.com`
before running either command. Supported hosts are `outlook.live.com`,
`outlook.office.com`, `outlook.office365.com`, and `outlook.cloud.microsoft`.

Each run prints the resolved profile directory. If sign-in is requested again,
confirm this path is unchanged and that both the browser profile and `auth.json`
exist. The reader injects saved cookies before navigating and refreshes the
cookie state after a successful read. Microsoft may still expire or invalidate
a session through MFA or Conditional Access; Playwright cannot bypass those
requirements. If Microsoft offers a "Stay signed in" prompt, accept it only if
your organization permits it. Complete the authorized sign-in again using the
probe if prompted. Do not delete the existing browser profile to apply this
change; run the probe against that same profile to create the initial cookie
state file after successful sign-in.

The probe does not use Microsoft Graph, register an application, read or print
message bodies, or send, modify, or delete email. It reports whether the mailbox
page and message rows are present, whether the first row has an accessible label
(without printing the label), and optionally whether an opened message body
container is visible. That optional manual step opens a message and Outlook may
mark it as read; skip it when mailbox state must remain unchanged.

## Read visible inbox messages

Run the reader from the project directory. It uses the installed Google Chrome
browser; install Chrome before running it:

```sh
python3 -m app.browser.outlook_reader --limit 50 --timeout-ms 30000
```

The reader reuses the persistent profile and `MOVEOUT_BROWSER_PROFILE` override,
restores saved cookies when present, and navigates to the Inbox on the configured
Outlook host. If switching
from Playwright's bundled Chromium, preserve the old profile by renaming
`browser_data/outlook/` to `browser_data/outlook.old/` before the first Chrome
run; the browser engines may not be able to share a profile. If Outlook
redirects to sign-in, the reader displays a prompt and waits while you complete
the entire sign-in, including email, password, and MFA, manually in the browser.
After you press Enter, the reader confirms that the browser reached Outlook,
waits for visible rows or an empty-Inbox notice, and saves fresh cookies before
reading. If Microsoft redirects back to sign-in while the reader is waiting for
rows, it prompts for sign-in recovery (up to two times), then stops with a
clear authentication error if Outlook still does not return. Complete any
remaining sign-in/MFA step in Chrome before pressing Enter. Run it directly; the
feasibility probe is not required for this login flow.

To disable interactive login (for example, in a scheduled process), use:

```sh
python3 -m app.browser.outlook_reader --limit 50 --timeout-ms 15000 --no-login
```

In this mode, an unauthenticated session raises an authentication error instead
of prompting. The reader waits for the Outlook message list, returns an empty
JSON array when the inbox visibly reports that it is empty, and reports a clear
timeout or authentication error otherwise.

To read visible Sent Items rows through the same authorized Playwright session,
call the reader directly:

```python
import asyncio

from app.browser.outlook_reader import read_sent_items

messages = asyncio.run(read_sent_items(limit=100))
print(f"Visible sent rows: {len(messages)}")
```

Like the Inbox reader, this only returns currently visible rows and exposed
preview fields; it does not search the complete Sent Items history.

The reader only inspects the visible message-list rows. It never clicks or opens
a message, so it avoids causing Outlook to mark an unread email as read. It
returns fields exposed by the current list-row accessibility/DOM data and uses
`null` for unavailable fields. `bodyPreview` is only populated when Outlook
exposes a preview in the row; the full message body, recipients, Graph message
ID, Internet Message ID, and a message link may not be exposed by the inbox list.
It does not paginate or fetch messages outside the currently rendered list.
Outlook's DOM is not a supported API, so inspect the output against the actual
authorized account before relying on any field mapping.

The JSON is printed to the terminal and may contain personal information.
Protect terminal output and do not save or share it without authorization.

## Deduplication persistence

SQLite remains the default. To use the optional local PostgreSQL service, copy
`.env.example` to `.env`, set a private `POSTGRES_PASSWORD`, install updated
requirements, then start PostgreSQL:

```sh
make db-up
```

Set both `MOVEOUT_DEDUP_DB` and `MOVEOUT_PROPERTY_DEDUP_DB` to the same
`postgresql://` connection string. The application creates the message,
property, send-audit, and sent-items-cache tables on first use. Configure the
same connection string for both variables; mismatched backends or PostgreSQL
URLs are rejected rather than silently splitting state.

To copy the legacy SQLite `seen_messages` and `processed_properties` tables to
PostgreSQL without deleting the SQLite source files:

```sh
.venv/bin/python -m scripts.migrate_sqlite_to_pg \
  --postgres-url "$MOVEOUT_DEDUP_DB" \
  --message-sqlite browser_data/outlook/message_dedup.sqlite3 \
  --property-sqlite browser_data/outlook/property_dedup.sqlite3
```

Retain the SQLite files as a backup and manage their retention manually. The
new audit tables store message subjects/previews, recipients, and dry-run
payloads; protect database access and backups accordingly. Stop the local
PostgreSQL service with `make db-down` (the named data volume is retained).

## Run the dry-run pipeline from Outlook

Use the same Chrome profile and visible-row reader to pass inbox messages
directly through the local pipeline:

```sh
.venv/bin/python -m app.main --input-live --limit 20
```

The command may prompt you to complete Outlook sign-in and MFA in Chrome. It
only reads visible inbox rows, then prints a dry-run action preview; it does not
send email, create THM tickets, WhatsApp messages, or calendar events. The
pipeline may send move-out subject/preview text to Gemini when
`GEMINI_API_KEY` is configured, and queries THM when `THM_PHONE` and
`THM_PASSWORD` are configured. Set those THM credentials in `.env`; the
read-only lookup logs in through `/api/login`, searches homes by tenant email
and then flat/society, and fetches the matched home's contacts, project,
association, category, rent, deposit, and lease expiry. No THM credentials are
stored in source code. The resulting terminal summary can include personal
information.
Use `--input-json path/to/messages.json` instead to run against a local fixture.

For the current THM data test, use lookup-only mode instead:

```sh
MOVEOUT_DRY_RUN=true \
MOVEOUT_DISABLE_TICKETS=true \
MOVEOUT_DISABLE_WHATSAPP=true \
MOVEOUT_DISABLE_CALENDAR=true \
  .venv/bin/python -m app.main --input-live --limit 20 --thm-lookup-only
```

This mode reads visible Outlook rows, parses matching notices, looks up the
property in THM, and prints `thm_matches`, `needs_review`, and `ignored_noise`.
It does not perform deduplication writes, build a pack, record planned sends,
or run ticket/email/WhatsApp/calendar actions. When `GEMINI_API_KEY` is set,
the parser uses Gemini and reports `parseMethod: "gemini"`; on an expected
Gemini request/response error it falls back to regex and reports
`parseMethod: "regex_fallback"`. Regex fills any required fields missing from
a successful Gemini response, reported as `parseMethod: "gemini+regex"`.
Without the key it uses regex and reports `parseMethod: "regex"`. Outlook's
list reader exposes only visible row data and preview text, not the full email
body.

To inspect visible inbox rows only, without parsing, THM lookup, deduplication,
or downstream actions:

```sh
.venv/bin/python -m app.main --read-only --limit 5
```

For a full pipeline dry-run that ignores prior message/property dedup state,
uses temporary SQLite stores, and logs one decision per inbox row:

```sh
MOVEOUT_DRY_RUN=true \
MOVEOUT_DISABLE_TICKETS=true \
MOVEOUT_DISABLE_WHATSAPP=true \
MOVEOUT_DISABLE_CALENDAR=true \
  .venv/bin/python -m app.main --input-live --limit 20 --fresh
```

The temporary dedup files are deleted when the command exits. `--fresh` does
not override the 24-hour stale-message filter or the 60-day property check
within that single run.

The startup banner reports the current channel configuration.
`MOVEOUT_DRY_RUN=true` (the default) previews actions. Setting it to `false`
does not enable live operations: execution stops with an error because live
adapters are not implemented. Email remains dry-run only. Set
`MOVEOUT_DISABLE_TICKETS=true`, `MOVEOUT_DISABLE_WHATSAPP=true`, and
`MOVEOUT_DISABLE_CALENDAR=true` to suppress those channel previews; their
planned pack contents remain available in the JSON output. These switches do
not enable live sending, and `execute_pack(dry_run=False)` is intentionally
rejected.

Example shape using anonymized sample values:

```json
[
  {
    "id": "sample-message-id",
    "internetMessageId": "<sample@example.net>",
    "subject": "Move-out date confirmation",
    "from": {
      "emailAddress": {
        "name": "Jordan Lee",
        "address": "jordan@example.net"
      }
    },
    "receivedDateTime": "2026-11-07T10:15:00Z",
    "toRecipients": [],
    "bodyPreview": "Please confirm the handover date.",
    "webLink": "https://outlook.live.com/mail/inbox/id/sample"
  }
]
```

## Run tests

```sh
python3 -m pytest -q
```

## Current migration boundary

The inbox reader does not classify messages or implement deduplication. The
local app has an optional, read-only THM lookup and a local move-out pack
generator. The THM endpoints and response fields follow the existing workflow
reference in `n8n_context_code.md`; verify them with a live authorized account
before relying on the result. The lookup does not use mock property records.
The pack's ticket categories and notification content are prototype
templates, not confirmed production Engine1/CFG rules. No ticket, email,
WhatsApp, Excel, or calendar writes are performed; `app.adapters.action_sender`
only prints redacted dry-run previews and reports planned counts. Confirm the
full Engine1/CFG rules, THM API contract, workbook, and communication mappings
before enabling those integrations.
# AE_move_Out
