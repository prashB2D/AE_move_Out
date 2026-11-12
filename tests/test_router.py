import json

import pytest

from app.logic import router
from app.logic.router import process_incoming_email


def test_routes_move_out_without_a_thm_match_to_manual_review(monkeypatch):
    monkeypatch.setattr(router, "thm_lookup", lambda *_args: router._not_found())
    email = {
        "id": "12345",
        "subject": "Move-Out Notice - Flat 302, Prestige Park",
        "bodyPreview": (
            "Move-out for Flat 302 at Prestige Park. "
            "My lease ends on November 30, 2026."
        ),
        "from": {
            "emailAddress": {"name": "John Doe", "address": "john@example.com"}
        },
    }

    result = process_incoming_email(email)

    assert result["route"] == "manual_review"
    assert result["data"]["thm_home_id"] is None


def test_routes_unknown_society_to_manual_review(monkeypatch):
    monkeypatch.setattr(router, "thm_lookup", lambda *_args: router._not_found())
    email = {
        "id": "67890",
        "subject": "Move-Out Notice - Flat 101, Unknown Towers",
        "bodyPreview": "Move-out for Flat 101 at Unknown Towers. Lease ends Dec 1, 2026.",
        "from": {
            "emailAddress": {"name": "Jane", "address": "jane@example.com"}
        },
    }

    assert process_incoming_email(email)["route"] == "manual_review"


def test_ignores_noise_email():
    email = {
        "id": "999",
        "subject": "Deployment status SUCCESS",
        "bodyPreview": "All good.",
        "from": {
            "emailAddress": {"name": "System", "address": "sys@example.com"}
        },
    }

    assert process_incoming_email(email)["route"] == "ignored"


def test_thm_lookup_authenticates_and_fetches_related_property_data(monkeypatch):
    monkeypatch.delenv("MOVEOUT_THM_EMAIL_LOOKUP", raising=False)
    monkeypatch.setenv("THM_PHONE", "test-phone")
    monkeypatch.setenv("THM_PASSWORD", "test-password")
    calls: list[tuple[str, str, object]] = []

    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.value

    class Client:
        def __init__(self, *, base_url: str, timeout: int) -> None:
            assert base_url == router.THM_BASE_URL
            assert timeout == router.THM_TIMEOUT_SECONDS

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, path: str, *, json: dict[str, str]):
            calls.append(("POST", path, json))
            return Response({"token": "test-token"})

        def get(self, path: str, *, params=None, headers):
            calls.append(("GET", path, headers))
            assert headers == {"Authorization": "test-token"}
            responses = {
                "/api/home": {
                    "rows": [
                        {
                            "_id": "home-1",
                            "name": "Prestige Park",
                            "displayName": "Prestige Park 302",
                            "tenancyTerms": {},
                            "sharingType": {"_id": "category-1"},
                            "owners": [{"_id": "owner-1"}],
                            "tenants": [{"_id": "tenant-1"}],
                            "tenantObjs": [],
                        }
                    ],
                    "count": 1,
                },
                "/api/home/home-1": {
                    "home": {
                        "_id": "home-1",
                        "name": None,
                        "displayName": "",
                        "tenancyTerms": {
                            "rentFromTenant": 66000,
                            "securityDeposit": 250000,
                            "contractPeriodEndDate": "2027-09-09T00:00:00Z",
                        },
                        "sharingType": {"_id": "category-1"},
                        "owners": [{"_id": "owner-1"}],
                        "tenants": [{"_id": "tenant-1"}],
                        "tenantObjs": [],
                        "listing": {"_id": "listing-1"},
                        "project": "project-1",
                    }
                },
                "/api/category/category-1": {"category": {"name": "PM"}},
                "/api/user/owner-1": {
                    "user": {
                        "fullName": "Owner One",
                        "email": "owner@example.com",
                        "phoneNumber": "9886004645",
                    }
                },
                "/api/user/tenant-1": {
                    "tenant": {
                        "fullName": "Tenant One",
                        "email": "tenant@example.com",
                        "phoneNumber": "8884764357",
                    }
                },
                "/api/listing/listing-1": {
                    "listing": {"project": {"_id": "project-1"}}
                },
                "/api/project/project-1": {
                    "project": {
                        "companyInfo": {
                            "companyPhoneNumber": 8042156169,
                            "companyCountryCode": "+91",
                        },
                        "customFields": [
                            {
                                "fieldLabel": "Email ID",
                                "feildValue": "gm@example.com, em@example.com",
                            }
                        ]
                    }
                },
            }
            return Response(responses[path])

    monkeypatch.setattr(router.httpx, "Client", Client)

    result = router.thm_lookup("Prestige Park", "302", "tenant@example.com")

    assert calls[0] == (
        "POST",
        "/api/login",
        {
            "countryCode": "+91",
            "phoneNumber": "test-phone",
            "password": "test-password",
        },
    )
    assert calls[1][0:2] == ("GET", "/api/home")
    assert result["thm_home_id"] == "home-1"
    assert result["thm_project_id"] == "project-1"
    assert result["thm_listing_id"] == "listing-1"
    assert result["society"] == "Prestige Park"
    assert result["flat"] == "302"
    assert result["ownerEmail"] == "owner@example.com"
    assert result["ownerPhone"] == "9886004645"
    assert result["tenantName"] == "Tenant One"
    assert result["tenantEmail"] == "tenant@example.com"
    assert result["tenantPhone"] == "8884764357"
    assert result["associationEmail"] == "gm@example.com; em@example.com"
    assert result["associationName"] == ""
    assert result["associationPhone"] == "+91 8042156169"
    assert result["thm_category"] == result["propertyType"] == "PM"
    assert result["monthlyRent"] == 66000
    assert result["securityDeposit"] == 250000
    assert result["leaseExpiry"] == "2027-09-09"
    assert result["thm_status"] == (
        "Matched THM home Prestige Park 302 (PM)."
    )
    assert not any(
        path.startswith("/api/user-tenant/")
        for method, path, _value in calls
        if method == "GET"
    )


@pytest.mark.parametrize(
    ("setting", "expected_email_search"),
    [
        (None, True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_email_lookup_toggle_controls_email_search(
    monkeypatch,
    capsys,
    setting,
    expected_email_search,
):
    if setting is None:
        monkeypatch.delenv("MOVEOUT_THM_EMAIL_LOOKUP", raising=False)
    else:
        monkeypatch.setenv("MOVEOUT_THM_EMAIL_LOOKUP", setting)

    queries = []
    matched_home = {
        "_id": "home-1",
        "displayName": "Sobha Daisy 3033",
        "name": "",
    }

    def fake_query_homes(_client, _token, query, *, max_pages):
        queries.append(query)
        if query == "3033":
            return [matched_home], 1
        return [], 0

    monkeypatch.setattr(router, "_query_homes", fake_query_homes)

    result = router._find_home(
        object(),
        "test-token",
        "Sobha Daisy",
        "3033",
        "dummy@example.com",
    )

    assert result == matched_home
    assert ("dummy@example.com" in queries) is expected_email_search
    assert "3033" in queries
    if not expected_email_search:
        assert "[thm] email lookup disabled" in capsys.readouterr().out


def test_thm_lookup_prefers_tenant_objects_without_extra_user_request(monkeypatch):
    monkeypatch.setenv("THM_PHONE", "test-phone")
    monkeypatch.setenv("THM_PASSWORD", "test-password")
    calls = []

    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            return None

        def json(self):
            return self.value

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response({"token": "test-token"})

        def get(self, path, *, params=None, headers):
            calls.append(path)
            if path == "/api/home":
                return Response(
                    {
                        "rows": [
                            {
                                "_id": "home-tenant-objs",
                                "displayName": "Example Heights 12",
                                "tenants": [{"_id": "stale-tenant-id"}],
                            }
                        ],
                        "count": 1,
                    }
                )
            if path == "/api/home/home-tenant-objs":
                return Response(
                    {
                        "home": {
                            "_id": "home-tenant-objs",
                            "tenantObjs": [
                                {
                                    "firstName": "Tenant",
                                    "lastName": "Objects",
                                    "email": "tenant-objects@example.com",
                                    "phoneNumber": "1234567890",
                                }
                            ],
                            "tenants": [{"_id": "stale-tenant-id"}],
                        }
                    }
                )
            raise AssertionError(f"Unexpected THM request: {path}")

    monkeypatch.setattr(router.httpx, "Client", Client)

    result = router.thm_lookup("Example Heights", "12", "sender@example.com")

    assert result["tenantName"] == "Tenant Objects"
    assert result["tenantEmail"] == "tenant-objects@example.com"
    assert result["tenantPhone"] == "1234567890"
    assert "/api/user-tenant/stale-tenant-id" not in calls
    assert "/api/user/stale-tenant-id" not in calls


def test_thm_test_fixture_matches_sender_without_http_calls(monkeypatch, capsys):
    fixture = {
        "test_sender_email": " Test@Example.com ",
        "thm_home_id": "fake-home",
        "thm_project_id": "fake-project",
        "society": "Example Heights",
        "flat": "12",
        "tenantName": "Test Tenant",
        "tenantEmail": "tenant@example.test",
        "tenantPhone": None,
        "ownerName": None,
        "ownerEmail": None,
        "ownerPhone": None,
        "thm_status": "matched",
    }
    monkeypatch.setenv("MOVEOUT_THM_TEST_MATCH", json.dumps(fixture))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Fixture match must not make THM HTTP calls.")

    monkeypatch.setattr(router.httpx, "Client", fail_if_called)

    result = router.thm_lookup(
        "Example Heights",
        "12",
        "test@example.com",
    )

    assert result == {
        key: value
        for key, value in fixture.items()
        if key != "test_sender_email"
    }
    assert result["thm_status"] == "matched"
    assert "[thm] test-mode match: test@example.com -> fake-home" in (
        capsys.readouterr().out
    )


def test_thm_test_fixture_nonmatching_sender_uses_http(monkeypatch):
    monkeypatch.setenv(
        "MOVEOUT_THM_TEST_MATCH",
        json.dumps(
            {
                "test_sender_email": "test@example.com",
                "thm_home_id": "fake-home",
            }
        ),
    )
    monkeypatch.setenv("THM_PHONE", "test-phone")
    monkeypatch.setenv("THM_PASSWORD", "test-password")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "test-token"} if calls[-1] == "POST" else {
                "rows": [],
                "count": 0,
            }

    class Client:
        def __init__(self, **_kwargs):
            calls.append("CLIENT")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            calls.append("POST")
            return Response()

        def get(self, *_args, **_kwargs):
            calls.append("GET")
            return Response()

    monkeypatch.setattr(router.httpx, "Client", Client)

    result = router.thm_lookup("Example Heights", "12", "other@example.com")

    assert calls[:2] == ["CLIENT", "POST"]
    assert "GET" in calls
    assert result == {"thm_home_id": None, "thm_status": "not_found"}


def test_thm_lookup_without_fixture_uses_http(monkeypatch):
    monkeypatch.delenv("MOVEOUT_THM_TEST_MATCH", raising=False)
    monkeypatch.setenv("THM_PHONE", "test-phone")
    monkeypatch.setenv("THM_PASSWORD", "test-password")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "test-token"} if calls[-1] == "POST" else {
                "rows": [],
                "count": 0,
            }

    class Client:
        def __init__(self, **_kwargs):
            calls.append("CLIENT")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            calls.append("POST")
            return Response()

        def get(self, *_args, **_kwargs):
            calls.append("GET")
            return Response()

    monkeypatch.setattr(router.httpx, "Client", Client)

    result = router.thm_lookup("Example Heights", "12", "test@example.com")

    assert "CLIENT" in calls
    assert result == {"thm_home_id": None, "thm_status": "not_found"}


def test_thm_test_fixture_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("MOVEOUT_THM_TEST_MATCH", "not-json")

    with pytest.raises(ValueError, match="MOVEOUT_THM_TEST_MATCH"):
        router.thm_lookup("Example Heights", "12", "test@example.com")


def test_thm_lookup_requires_login_credentials(monkeypatch):
    monkeypatch.delenv("MOVEOUT_THM_TEST_MATCH", raising=False)
    monkeypatch.delenv("THM_PHONE", raising=False)
    monkeypatch.delenv("THM_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="THM_PHONE and THM_PASSWORD"):
        router.thm_lookup("Prestige Park", "302", "tenant@example.com")


def test_thm_lookup_returns_not_found_when_property_is_missing(monkeypatch):
    monkeypatch.setenv("THM_PHONE", "test-phone")
    monkeypatch.setenv("THM_PASSWORD", "test-password")

    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            return None

        def json(self):
            return self.value

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response({"token": "test-token"})

        def get(self, *_args, **_kwargs):
            return Response({"rows": [], "count": 0})

    monkeypatch.setattr(router.httpx, "Client", Client)

    assert router.thm_lookup("Unknown Society", "999") == {
        "thm_home_id": None,
        "thm_status": "not_found",
    }
