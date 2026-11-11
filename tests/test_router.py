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
                        }
                    ],
                    "count": 1,
                },
                "/api/home/home-1": {
                    "home": {
                        "_id": "home-1",
                        "name": "Prestige Park 302",
                        "tenancyTerms": {
                            "rentFromTenant": 66000,
                            "securityDeposit": 250000,
                            "contractPeriodEndDate": "2027-09-09T00:00:00Z",
                        },
                        "sharingType": {"_id": "category-1"},
                        "owners": [{"_id": "owner-1"}],
                        "tenants": [{"_id": "tenant-1"}],
                        "listing": {"_id": "listing-1"},
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
                "/api/user-tenant/tenant-1": {
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
                        "customFields": [
                            {
                                "fieldLabel": "Association Email",
                                "fieldValue": "gm@example.com; em@example.com",
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
    assert result["ownerEmail"] == "owner@example.com"
    assert result["ownerPhone"] == "9886004645"
    assert result["tenantEmail"] == "tenant@example.com"
    assert result["tenantPhone"] == "8884764357"
    assert result["associationEmail"] == "gm@example.com; em@example.com"
    assert result["thm_category"] == result["propertyType"] == "PM"
    assert result["monthlyRent"] == 66000
    assert result["securityDeposit"] == 250000
    assert result["leaseExpiry"] == "2027-09-09"
    assert result["thm_status"] == "matched"


def test_thm_lookup_requires_login_credentials(monkeypatch):
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

