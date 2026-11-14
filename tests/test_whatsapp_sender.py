import asyncio
import json

import httpx
import pytest

from app.adapters import whatsapp_sender as w
from app.adapters.notifier import Followup, LogNotifier, build_email_followups

FOLLOWUP = Followup(role="owner", phone="4694014747", message="", email_subject="Notice",
                    name="ANIL KRISHNAN", property="Krishna Brookfield T-17")


def _config(monkeypatch, **env):
    base = {"META_WA_ACCESS_TOKEN": "secret-token", "META_WA_PHONE_NUMBER_ID": "123",
            "MOVEOUT_WA_TEST_NUMBER": "+91 90000 11111"}
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    return w.WhatsAppConfig()


def _client(record, status=200, body=None):
    def handler(request):
        record.append(request)
        return httpx.Response(status, json=body or {"messages": [{"id": "wamid.1"}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("raw, expected", [
    ("9667215550", "919667215550"), ("+91 96672-15550", "919667215550"),
    ("919667215550", "919667215550"), ("", "")])
def test_normalize_phone(raw, expected) -> None:
    assert w.normalize_phone(raw) == expected


def test_test_number_receives_every_message(monkeypatch, capsys) -> None:
    config = _config(monkeypatch, MOVEOUT_WA_TEMPLATE_OWNER="renewal_offer_owner",
                     MOVEOUT_WA_TEMPLATE_LANG="en", MOVEOUT_WA_TEMPLATE_PARAMS="name,property,company")
    sent = []
    asyncio.run(w.WhatsAppNotifier(config, _client(sent)).notify(FOLLOWUP))
    request = sent[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://graph.facebook.com/v21.0/123/messages"
    assert request.headers["Authorization"] == "Bearer secret-token"
    assert body["to"] == "919000011111"  # test number, not the owner's
    assert body["template"]["name"] == "renewal_offer_owner"
    assert body["template"]["language"] == {"code": "en"}
    assert [p["text"] for p in body["template"]["components"][0]["parameters"]] == [
        "ANIL KRISHNAN", "Krishna Brookfield T-17", "Agarwal Estates"]
    out = capsys.readouterr().out
    assert "secret-token" not in out and "1111" in out and "90000" not in out


def test_hello_world_default_has_no_variables(monkeypatch) -> None:
    config = _config(monkeypatch)
    payload = w.build_payload(FOLLOWUP, config, "91x")
    assert payload["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def test_real_numbers_only_when_live(monkeypatch, capsys) -> None:
    config = _config(monkeypatch, MOVEOUT_WA_TEST_NUMBER="")
    sent = []
    asyncio.run(w.WhatsAppNotifier(config, _client(sent)).notify(FOLLOWUP))
    assert sent == [] and "Not sent" in capsys.readouterr().out
    config = _config(monkeypatch, MOVEOUT_WA_TEST_NUMBER="", MOVEOUT_WA_LIVE="true")
    asyncio.run(w.WhatsAppNotifier(config, _client(sent)).notify(FOLLOWUP))
    assert json.loads(sent[0].content)["to"] == "914694014747"


def test_api_error_is_raised_with_meta_message(monkeypatch) -> None:
    config = _config(monkeypatch)
    error = {"error": {"code": 131030, "message": "Recipient phone number not in allowed list"}}
    with pytest.raises(RuntimeError, match="131030: Recipient phone number not in allowed list"):
        asyncio.run(w.WhatsAppNotifier(config, _client([], 400, error)).notify(FOLLOWUP))


def test_notifier_from_env_falls_back_to_logging(monkeypatch) -> None:
    assert isinstance(w.notifier_from_env(), LogNotifier)  # disabled
    monkeypatch.setenv("MOVEOUT_WA_ENABLED", "true")
    assert isinstance(w.notifier_from_env(), LogNotifier)  # no token
    _config(monkeypatch)
    assert isinstance(w.notifier_from_env(), w.WhatsAppNotifier)


def test_bad_template_param_name_rejected(monkeypatch) -> None:
    with pytest.raises(ValueError, match="unknown field"):
        _config(monkeypatch, MOVEOUT_WA_TEMPLATE_PARAMS="name,phone")


def test_followup_carries_name_and_property() -> None:
    pack = {"ownerPhone": "4694014747", "propertyRef": "Krishna Brookfield T-17",
            "whatsappMessages": [{"role": "owner", "variables": ["ANIL KRISHNAN", "x"]}]}
    followup = build_email_followups({"role": "owner", "subject": "S"}, pack)[0]
    assert (followup.name, followup.property) == ("ANIL KRISHNAN", "Krishna Brookfield T-17")
