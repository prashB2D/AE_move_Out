import json

import httpx
import pytest

from app.adapters import thm_ticket_creator as t

HOME, PROJECT = "home1", "proj1"


def _ticket(category, sub=None, job="2026-11-13"):
    ticket = {"home": HOME, "project": PROJECT, "ticketCategory": category,
              "ticketBelongsTo": "Home", "ticketBelongsToRefId": HOME,
              "status": "open", "jobDate": job}
    if sub:
        ticket["subCategory"] = sub
    return ticket


TICKETS = [_ticket("Home Cleaning", "Deep cleaning"), _ticket("Painting"),
           _ticket("Home Cleaning", "Chimney cleaning"), _ticket("Unknown Thing")]


class FakeTHM:
    def __init__(self, existing=(), fail_post_for=(), ignore_filter=False, page_size=500):
        self.ignore_filter = ignore_filter
        self.page_size = page_size
        self.posts = []
        self.existing = list(existing)
        self.fail_post_for = set(fail_post_for)
        self.auth_headers = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/login":
            return httpx.Response(200, json={"token": "tok"})
        self.auth_headers.add(request.headers.get("Authorization"))
        if path == "/api/category":
            return httpx.Response(200, json={"rows": [
                {"_id": "c-clean", "name": "Home Cleaning", "module": "tickets",
                 "subCategories": [{"_id": "s-deep", "name": "Deep cleaning"},
                                   {"_id": "s-chim", "name": "Chimney Cleaning"}]},
                {"_id": "c-paint", "name": "Painting"},
                {"_id": "c-other", "name": "Leads", "module": "leads"}]})
        if path == "/api/ticket" and request.method == "GET":
            if self.ignore_filter or "ticketBelongsToRefId" not in request.url.params:
                return httpx.Response(200, json={"count": 15476, "rows": [{}] * 30})
            rows = self.existing[: self.page_size]
            return httpx.Response(200, json={"count": len(self.existing), "rows": rows})
        if path == "/api/ticket" and request.method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if body["ticketCategory"] in self.fail_post_for:
                return httpx.Response(400, json={"message": "bad"})
            return httpx.Response(200, json={"ticket": {"_id": f"t{len(self.posts)}"}})
        return httpx.Response(404)


def _client(fake):
    thm = t.THMClient(httpx.Client(base_url="https://thm.test",
                                   transport=httpx.MockTransport(fake.handler)))
    thm.login("1", "pw", "+91")
    return thm


def test_posts_resolved_ids_and_reports_unknown_category() -> None:
    fake = FakeTHM()
    results = t.create_tickets(TICKETS, _client(fake), log=lambda _m: None)
    assert [p["ticketCategory"] for p in fake.posts] == ["c-clean", "c-paint", "c-clean"]
    assert fake.posts[0]["ticketSubCategory"] == "s-deep"
    assert fake.posts[2]["ticketSubCategory"] == "s-chim"  # case-insensitive match
    assert all("subCategory" not in p for p in fake.posts)  # raw name never sent
    assert fake.auth_headers == {"tok"}  # raw token, no "Bearer"
    assert results[3] == {"ticketCategory": "Unknown Thing", "ok": False,
                          "error": 'category not found in project: "Unknown Thing"'}


def test_shortfall_skips_already_open_and_ignores_closed() -> None:
    fake = FakeTHM(existing=[
        {"ticketCategory": "Home cleaning", "status": "open"},  # live THM: a name
        {"ticketCategory": "Painting", "status": "rejected"},  # closed: doesn't count
    ])
    results = t.create_tickets(TICKETS[:3], _client(fake), log=lambda _m: None)
    # 2 cleaning wanted, 1 open -> create 1; painting closed -> create 1
    assert [p["ticketCategory"] for p in fake.posts] == ["c-clean", "c-paint"]
    assert results[2] == {"ticketCategory": "Home Cleaning", "ok": True, "skipped": True}


def test_rerun_with_everything_open_creates_nothing() -> None:
    fake = FakeTHM(existing=[{"ticketCategory": "Home cleaning", "status": "open"}] * 2
                   + [{"ticketCategory": {"_id": "c-paint"}, "status": "open"}])
    t.create_tickets(TICKETS[:3], _client(fake), log=lambda _m: None)
    assert fake.posts == []


def test_one_failed_post_does_not_stop_the_rest() -> None:
    fake = FakeTHM(fail_post_for={"c-paint"})
    results = t.create_tickets(TICKETS[:3], _client(fake), log=lambda _m: None)
    assert [r["ok"] for r in results] == [True, False, True]
    assert "HTTP 400" in results[1]["error"]
    remark = t.ticket_remark(results)
    assert remark.startswith("Created: Home Cleaning (t1), Home Cleaning (t3) | Failed: Painting [HTTP 400")


def _pack_file(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps({"emailsToSend": [], "tickets": TICKETS[:2]}))
    return str(path)


def test_dry_run_is_default_and_makes_no_http(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(t.httpx, "Client", None)  # any HTTP client use would crash
    assert t.main(["--pack-file", _pack_file(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Dry-run: 2 tickets would be created for home home1" in out
    assert "Home Cleaning (Deep cleaning) - jobDate 2026-11-13" in out
    assert "No tickets created." in out


def test_live_requires_exact_confirmation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(t, "_client_from_env",
                        lambda: pytest.fail("must not log in without confirmation"))
    code = t.main(["--pack-file", _pack_file(tmp_path), "--live"],
                  confirm=lambda _p: "create tickets")
    assert code == 1 and "Nothing created" in capsys.readouterr().out


@pytest.mark.parametrize("fake, message", [
    (FakeTHM(ignore_filter=True), "ignored the per-home ticket filter"),
    (FakeTHM(existing=[{"ticketCategory": "Painting", "status": "open"}] * 3, page_size=2),
     "only 2 were listed"),
])
def test_unreliable_duplicate_check_creates_nothing(fake, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        t.create_tickets(TICKETS[:3], _client(fake), log=lambda _m: None)
    assert fake.posts == []
