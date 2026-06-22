"""portfolio console view tests (ADR 0026) — the dashboard page + its routers.

Host-free: the page is a static string (four-rules-checked), and the data route is
mounted into a bare FastAPI app with the rollup helpers stubbed. No fleet, no HTTP.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import portfolio
from portfolio import view


# ── register() wires BOTH routers at distinct prefixes ───────────────────────────


def test_register_wires_both_view_routers():
    seen = []

    class _Reg:
        config = {}

        def register_tool(self, t):
            pass

        def register_router(self, router, prefix):
            seen.append(prefix)

    portfolio.register(_Reg())
    assert "/plugins/portfolio" in seen  # public page
    assert "/api/plugins/portfolio" in seen  # gated data


# ── the page meets the four rules ────────────────────────────────────────────────


def test_view_page_is_four_rules_compliant():
    html = view.VIEW_PAGE
    # Rule 4 — links the DS kit (CSS + dynamic ES-module import), uses --pl-* tokens
    assert "/_ds/plugin-kit.css" in html
    assert 'import(BASE + "/_ds/plugin-kit.js")' in html
    assert 'type="module"' in html
    assert "--pl-color-bg" in html
    # Rule 3 — slug-aware base, and data fetched via the kit's authed fetch
    assert 'location.pathname.split("/plugins/")[0]' in html
    assert "kit.apiFetch" in html
    assert "/api/plugins/portfolio/overview" in html
    assert "kit.initPluginView" in html
    # Must NOT hand-roll the theme or the handshake (the kit owns both)
    assert ":root{" not in html and ":root {" not in html
    assert 'addEventListener("message"' not in html


def test_view_page_has_tabs_drilldown_and_graph():
    html = view.VIEW_PAGE
    # two tabs
    assert 'data-tab="boards"' in html and 'data-tab="deps"' in html
    # cards are clickable → drill into a board's features
    assert "data-board" in html and "openBoard" in html
    assert "/api/plugins/portfolio/board/" in html
    # the dependency graph + plan
    assert "depGraph" in html and "/api/plugins/portfolio/plan" in html
    assert "<svg" in html and "marker-end" in html  # the SVG edges


def test_view_page_served_public_not_gated():
    app = FastAPI()
    app.include_router(view.build_view_router(), prefix="/plugins/portfolio")
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    c = TestClient(app)
    # the PAGE is public (iframe-loadable) …
    r = c.get("/plugins/portfolio/dashboard")
    assert r.status_code == 200 and "Portfolio" in r.text
    # … and NOT under /api (that would poison the slug-aware base)
    assert c.get("/api/plugins/portfolio/dashboard").status_code == 404


# ── the data route returns the rollup ────────────────────────────────────────────


@pytest.fixture
def overview_app(monkeypatch):
    """Mount the data router with the rollup helpers stubbed (no fleet/HTTP)."""
    from graph.fleet import supervisor

    monkeypatch.setattr(
        portfolio, "_load_teams", lambda: [{"name": "alpha", "repo": "/r", "port": 7901, "auto_dispose": True}]
    )
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [
            {"name": "host", "host": True},
            {"name": "alpha", "id": "alpha-1", "port": 7901},  # the spawned team (local member)
            {"name": "ext", "id": "ext-1", "remote": True, "url": "https://ext.example"},  # a standing remote
        ],
    )
    monkeypatch.setattr(
        portfolio,
        "_remote_by_name",
        lambda n: {
            "alpha": {"name": "alpha", "url": "http://127.0.0.1:7901", "token": ""},
            "ext": {"name": "ext", "url": "https://ext.example", "token": "t"},
        }.get(n),
    )

    async def fake_fetch(rec, state=""):
        if rec["name"] == "alpha":
            return [{"id": "1", "board_state": "done"}, {"id": "2", "board_state": "in_progress"}]
        return [{"id": "3", "board_state": "done"}]  # ext: drained

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)

    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    return TestClient(app)


def test_overview_rolls_up_every_board(overview_app):
    data = overview_app.get("/api/plugins/portfolio/overview").json()
    boards = {b["board"]: b for b in data["boards"]}
    assert set(boards) == {"alpha", "ext"}
    # the spawned team: ephemeral, repo + a2a, lane counts, not drained (1 active)
    a = boards["alpha"]
    assert a["spawned"] is True and a["repo"] == "/r" and a["reachable"] is True
    assert a["a2a"] == "http://127.0.0.1:7901/a2a"
    assert a["counts"] == {"done": 1, "in_progress": 1} and a["drained"] is False
    # the standing remote: not spawned, drained (its only feature is done)
    e = boards["ext"]
    assert e["spawned"] is False and e["drained"] is True


def test_overview_marks_unreachable_boards(overview_app, monkeypatch):
    from portfolio import _BoardUnavailable

    async def boom(rec, state=""):
        raise _BoardUnavailable("no board exposed")

    monkeypatch.setattr(portfolio, "_fetch_board_features", boom)
    data = overview_app.get("/api/plugins/portfolio/overview").json()
    for b in data["boards"]:
        assert b["reachable"] is False and "no board exposed" in b["error"]


def test_overview_empty_when_no_boards(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(portfolio, "_load_teams", lambda: [])
    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "host": True}])
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    assert TestClient(app).get("/api/plugins/portfolio/overview").json() == {"boards": []}


# ── /board/{name} — the card drill-down ──────────────────────────────────────────


def test_board_route_returns_full_feature_list(overview_app):
    d = overview_app.get("/api/plugins/portfolio/board/alpha").json()
    assert d["board"] == "alpha"
    assert [f["id"] for f in d["features"]] == ["1", "2"]  # the full list, untruncated


def test_board_route_unresolvable(monkeypatch):
    monkeypatch.setattr(portfolio, "_remote_by_name", lambda n: None)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).get("/api/plugins/portfolio/board/nope").json()
    assert d["features"] == [] and "not resolvable" in d["error"]


# ── /plan — the cross-board dependency graph ─────────────────────────────────────


def test_plan_route_reflects_compute_plan(monkeypatch):
    from graph.fleet import supervisor

    link = {"id": "lnk-1", "from_board": "web", "from_feature": "f2", "to_board": "api", "to_feature": "f1"}
    monkeypatch.setattr(portfolio, "_load_links", lambda: [link])
    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "a", "name": "api", "url": "http://api", "token": ""},
            {"id": "w", "name": "web", "url": "http://web", "token": ""},
        ],
    )

    async def fake_fetch(rec, state=""):
        return {"api": [{"id": "f1", "board_state": "done"}], "web": [{"id": "f2", "board_state": "ready"}]}[
            rec["name"]
        ]

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    plan = TestClient(app).get("/api/plugins/portfolio/plan").json()
    # the link's blocker (api·f1) is done → satisfied; web·f2 is ready to dispatch
    assert plan["links"][0]["status"] == "satisfied"
    assert plan["ready_to_dispatch"] == [{"board": "web", "feature": "f2", "state": "ready"}]
    assert plan["blocked"] == []


def test_plan_route_empty_when_no_links(monkeypatch):
    monkeypatch.setattr(portfolio, "_load_links", lambda: [])
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    assert TestClient(app).get("/api/plugins/portfolio/plan").json() == {
        "links": [],
        "ready_to_dispatch": [],
        "blocked": [],
    }


def test_plan_resolves_LOCAL_spawned_team_boards(monkeypatch):
    """Regression: the plan must see LOCAL spawned-team boards (status), not only remotes
    (list_remotes) — else a spawned team's links always read 'dangling'."""
    from graph.fleet import supervisor

    link = {"id": "l1", "from_board": "teamb", "from_feature": "b-ui", "to_board": "showteam", "to_feature": "f1"}
    monkeypatch.setattr(portfolio, "_load_links", lambda: [link])
    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])  # NO remotes — both are local
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [
            {"name": "host", "host": True},
            {"name": "showteam", "id": "s1", "port": 7873},
            {"name": "teamb", "id": "t1", "port": 7874},
        ],
    )

    async def fake_fetch(rec, state=""):
        return {"showteam": [{"id": "f1", "board_state": "ready"}], "teamb": []}.get(rec["name"], [])

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    plan = TestClient(app).get("/api/plugins/portfolio/plan").json()
    # showteam·f1 exists (ready, not done) → BLOCKING, not dangling
    assert plan["links"][0]["status"] == "blocking"
