"""WhatsApp notifications via the Meta WhatsApp Cloud API.

Used after an email is sent: "we have sent you an email, please check it".
Business-initiated WhatsApp messages must use an APPROVED template, so the
template name, language and body variables all come from .env.

Safety:
* MOVEOUT_WA_ENABLED must be "true", otherwise messages are only logged.
* MOVEOUT_WA_TEST_NUMBER, when set, receives EVERY message (test mode).
  Without it, real owner/tenant numbers are used only if MOVEOUT_WA_LIVE=true.
* The access token is never printed; phone numbers are masked in logs.

Quick check of your token/template (sends one message to the test number):
    python -m app.adapters.whatsapp_sender --test owner
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from typing import Any

import httpx

from app.adapters.notifier import Followup, LogNotifier, Notifier, _mask

GRAPH_URL = "https://graph.facebook.com"
_PARAM_FIELDS = ("name", "property", "company", "subject")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def normalize_phone(raw: str, country_code: str = "91") -> str:
    """'+91 96672-15550' / '9667215550' -> '919667215550' (digits, with country code)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = country_code + digits
    return digits


class WhatsAppConfig:
    def __init__(self) -> None:
        self.token = _env("META_WA_ACCESS_TOKEN")
        self.phone_number_id = _env("META_WA_PHONE_NUMBER_ID")
        self.api_version = _env("META_WA_API_VERSION", "v21.0")
        self.test_number = normalize_phone(_env("MOVEOUT_WA_TEST_NUMBER"))
        self.live = _env("MOVEOUT_WA_LIVE").lower() == "true"
        self.templates = {
            "owner": _env("MOVEOUT_WA_TEMPLATE_OWNER", "hello_world"),
            "tenant": _env("MOVEOUT_WA_TEMPLATE_TENANT", "hello_world"),
        }
        self.language = _env("MOVEOUT_WA_TEMPLATE_LANG", "en_US")
        params = _env("MOVEOUT_WA_TEMPLATE_PARAMS")
        self.params = [p.strip() for p in params.split(",") if p.strip()]
        self.company = _env("MOVEOUT_COMPANY_NAME", "Agarwal Estates")
        unknown = set(self.params) - set(_PARAM_FIELDS)
        if unknown:
            raise ValueError(f"MOVEOUT_WA_TEMPLATE_PARAMS: unknown field(s) {sorted(unknown)}; "
                             f"use {', '.join(_PARAM_FIELDS)}")

    def missing(self) -> list[str]:
        return [name for name, value in (("META_WA_ACCESS_TOKEN", self.token),
                                         ("META_WA_PHONE_NUMBER_ID", self.phone_number_id))
                if not value]


def build_payload(followup: Followup, config: WhatsAppConfig, to: str) -> dict[str, Any]:
    values = {
        "name": followup.name or "Sir/Madam",
        "property": followup.property or "your property",
        "company": config.company,
        "subject": followup.email_subject,
    }
    template: dict[str, Any] = {
        "name": config.templates.get(followup.role, config.templates["owner"]),
        "language": {"code": config.language},
    }
    if config.params:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": values[p][:1000]} for p in config.params],
        }]
    return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}


class WhatsAppNotifier:
    def __init__(self, config: WhatsAppConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client

    def recipient(self, followup: Followup) -> str | None:
        if self.config.test_number:
            return self.config.test_number
        if self.config.live:
            return normalize_phone(followup.phone)
        return None

    async def notify(self, followup: Followup) -> None:
        to = self.recipient(followup)
        if not to:
            print("[whatsapp] Not sent: set MOVEOUT_WA_TEST_NUMBER (test) or "
                  "MOVEOUT_WA_LIVE=true (real numbers).")
            return
        payload = build_payload(followup, self.config, to)
        url = (f"{GRAPH_URL}/{self.config.api_version}/"
               f"{self.config.phone_number_id}/messages")
        headers = {"Authorization": f"Bearer {self.config.token}"}
        client = self.client or httpx.AsyncClient(timeout=20)
        try:
            response = await client.post(url, json=payload, headers=headers)
        finally:
            if self.client is None:
                await client.aclose()
        redirected = " (test number)" if self.config.test_number else ""
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
                detail = f"{error.get('code')}: {error.get('message')}"
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(f"WhatsApp API HTTP {response.status_code} - {detail}")
        message_id = ((response.json().get("messages") or [{}])[0]).get("id", "?")
        print(f"[whatsapp] Sent '{payload['template']['name']}' to {followup.role} "
              f"{_mask(to)}{redirected} - id {message_id}")


def notifier_from_env() -> Notifier:
    """WhatsAppNotifier when enabled and configured, else the log-only notifier."""
    if _env("MOVEOUT_WA_ENABLED").lower() != "true":
        return LogNotifier()
    config = WhatsAppConfig()
    missing = config.missing()
    if missing:
        print(f"[whatsapp] MOVEOUT_WA_ENABLED=true but {', '.join(missing)} not set; "
              "logging only.")
        return LogNotifier()
    return WhatsAppNotifier(config)


def _main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Send one test WhatsApp notification.")
    parser.add_argument("--test", choices=("owner", "tenant"), required=True)
    args = parser.parse_args()
    config = WhatsAppConfig()
    if config.missing() or not config.test_number:
        print(f"[whatsapp] Set {', '.join(config.missing() + (['MOVEOUT_WA_TEST_NUMBER'] if not config.test_number else []))} in .env first.")
        return 2
    followup = Followup(role=args.test, phone=config.test_number, message="",
                        email_subject="Move-out notice", name="Test User",
                        property="Krishna Brookfield T-17")
    asyncio.run(WhatsAppNotifier(config).notify(followup))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
