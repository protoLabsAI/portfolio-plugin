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


def test_view_page_has_spinup_button():
    html = view.VIEW_PAGE
    assert 'id="newbtn"' in html and "openSpawn" in html  # the + Team button
    # the form POSTs to the spinup route via the kit's authed fetch
    assert "/api/plugins/portfolio/spinup" in html
    assert 'method: "POST"' in html
    assert 'name="repo"' in html and 'name="onboard"' in html  # the form fields


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


# ── POST /spinup — the dashboard button ──────────────────────────────────────────


def test_spinup_route_calls_the_shared_core(monkeypatch):
    captured = {}

    async def fake_spinup(name, repo="", *a, **k):
        captured.update(name=name, repo=repo, onboard=k.get("onboard"), auto_dispose=k.get("auto_dispose"))
        return {"team": name, "port": 7901, "ready": True}

    monkeypatch.setattr(portfolio, "_spinup_team", fake_spinup)
    app = FastAPI()
    app.include_router(view.build_data_router({"team_template": "/t"}), prefix="/api/plugins/portfolio")
    r = TestClient(app).post(
        "/api/plugins/portfolio/spinup", json={"name": "docs", "repo": "/r", "onboard": True, "auto_dispose": False}
    )
    assert r.json()["team"] == "docs"
    # the button's choices reach the shared core (onboard defaults to the posted value)
    assert captured == {"name": "docs", "repo": "/r", "onboard": True, "auto_dispose": False}


def test_spinup_route_surfaces_errors(monkeypatch):
    async def fake_spinup(name, repo="", *a, **k):
        return {"error": "Error: a team named 'docs' already exists."}

    monkeypatch.setattr(portfolio, "_spinup_team", fake_spinup)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/spinup", json={"name": "docs", "repo": "/r"}).json()
    assert "already exists" in d["error"]


def test_spinup_route_passes_archetype(monkeypatch):
    captured = {}

    async def fake_spinup(name, repo="", *a, **k):
        captured["archetype"] = k.get("archetype")
        return {"team": name}

    monkeypatch.setattr(portfolio, "_spinup_team", fake_spinup)
    app = FastAPI()
    app.include_router(view.build_data_router({}), prefix="/api/plugins/portfolio")
    TestClient(app).post("/api/plugins/portfolio/spinup", json={"name": "pc1", "archetype": "protocontent"})
    assert captured["archetype"] == "protocontent"


def test_archetypes_route_lists_presets():
    cfg = {"team_archetypes": {"protocontent": {"repo": "/dev/pc", "gate": "build"}}}
    app = FastAPI()
    app.include_router(view.build_data_router(cfg), prefix="/api/plugins/portfolio")
    d = TestClient(app).get("/api/plugins/portfolio/archetypes").json()
    assert d == {"archetypes": [{"name": "protocontent", "repo": "/dev/pc"}]}


def test_view_page_has_archetype_select():
    html = view.VIEW_PAGE
    assert "/api/plugins/portfolio/archetypes" in html  # the form fetches the presets
    assert 'name="archetype"' in html or "archetype" in html


# ── /forget — remove a dead board (#23) ──────────────────────────────────────────


def test_forget_disposes_a_spawned_team(monkeypatch):
    """A portfolio-spawned team is torn down via _dispose (stop + purge + unregister)."""
    disposed = {}

    def _fake_dispose(team):
        disposed["team"] = team["name"]
        return {"team": team["name"], "purged": True}

    monkeypatch.setattr(portfolio, "_team_by_name", lambda n: {"name": n, "id": f"{n}-id"} if n == "alpha" else None)
    monkeypatch.setattr(portfolio, "_dispose", _fake_dispose)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/forget", json={"board": "alpha"}).json()
    assert d["purged"] is True and disposed["team"] == "alpha"


def test_forget_drops_a_dead_local_member_not_in_registry(monkeypatch):
    """The Roxy case: the team is gone from the spawned-teams registry (teardown can't
    find it) but lingers as a dead local fleet member — _forget_board stops + purges it."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    calls = {}
    monkeypatch.setattr(portfolio, "_team_by_name", lambda n: None)  # not in the registry
    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [{"name": "host", "host": True}, {"name": "protocli-check", "id": "protocli-check-id", "port": 7871}],
    )
    monkeypatch.setattr(supervisor, "stop", lambda wid: calls.setdefault("stopped", wid))
    monkeypatch.setattr(manager, "remove", lambda wid, purge=False: calls.setdefault("removed", (wid, purge)))
    monkeypatch.setattr(portfolio, "_forget_team", lambda n: calls.setdefault("forgot", n))
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/forget", json={"board": "protocli-check"}).json()
    assert d["purged"] is True
    assert calls["removed"] == ("protocli-check-id", True) and calls["stopped"] == "protocli-check-id"


def test_forget_unregisters_a_remote(monkeypatch):
    from graph.fleet import supervisor

    dropped = {}
    monkeypatch.setattr(portfolio, "_team_by_name", lambda n: None)
    monkeypatch.setattr(supervisor, "list_remotes", lambda: [{"name": "ext", "id": "ext"}])
    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "host": True}])
    monkeypatch.setattr(supervisor, "remove_remote", lambda n: dropped.setdefault("name", n))
    monkeypatch.setattr(portfolio, "_forget_team", lambda n: None)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/forget", json={"board": "ext"}).json()
    assert d["unregistered"] is True and dropped["name"] == "ext"


def test_forget_already_gone_is_idempotent(monkeypatch):
    """A board with no backing record (already reaped server-side — the stale-card case)
    returns cleanly, so the dashboard's ✕ always clears the card."""
    from graph.fleet import supervisor

    monkeypatch.setattr(portfolio, "_team_by_name", lambda n: None)
    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "host": True}])
    monkeypatch.setattr(portfolio, "_forget_team", lambda n: None)
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/forget", json={"board": "ghost"}).json()
    assert d["forgotten"] is True and "already cleared" in d["note"]


def test_forget_requires_a_board_name(monkeypatch):
    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    d = TestClient(app).post("/api/plugins/portfolio/forget", json={}).json()
    assert "required" in d["error"]


def test_view_page_has_remove_affordance():
    html = view.VIEW_PAGE
    assert "data-forget" in html  # the ✕ on an unreachable card
    assert "/api/plugins/portfolio/forget" in html  # wired to the route


# ── ephemeral badge gated on auto_dispose (GH #26) ─────────────────────────────


def test_view_page_card_template_reads_auto_dispose_not_spawned():
    """The ephemeral badge must come from `b.auto_dispose`, not `b.spawned`.

    Regression for GH #26: the dashboard always showed "ephemeral" regardless of the
    auto_dispose flag. The fix gates the badge on `b.auto_dispose`.
    """
    html = view.VIEW_PAGE
    assert "b.auto_dispose" in html
    assert 'class="badge eph">ephemeral</span>' in html


def test_overview_exposes_auto_dispose_per_board(overview_app):
    """The overview API must include `auto_dispose` on each board record so the UI
    can render the ephemeral badge correctly."""
    data = overview_app.get("/api/plugins/portfolio/overview").json()
    boards = {b["board"]: b for b in data["boards"]}
    # alpha was mocked with auto_dispose=True → ephemeral badge
    assert boards["alpha"]["auto_dispose"] is True
    # ext is a standing remote (not in teams) → auto_dispose=False → no ephemeral badge
    assert boards["ext"]["auto_dispose"] is False


def test_overview_auto_dispose_false_means_no_ephemeral_badge(monkeypatch):
    """A team with auto_dispose=False must NOT get the ephemeral badge.

    Regression for GH #26. The card template renders `<span class="badge eph">ephemeral</span>`
    only when `b.auto_dispose` is truthy.
    """
    from graph.fleet import supervisor

    monkeypatch.setattr(
        portfolio,
        "_load_teams",
        lambda: [{"name": "persistent", "repo": "/r", "port": 7901, "auto_dispose": False}],
    )
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [{"name": "host", "host": True}, {"name": "persistent", "id": "p1", "port": 7901}],
    )
    monkeypatch.setattr(
        portfolio,
        "_remote_by_name",
        lambda n: {"persistent": {"name": n, "url": "http://127.0.0.1:7901", "token": ""}}.get(n),
    )

    async def fake_fetch(rec, state=""):
        return [{"id": "1", "board_state": "backlog"}]

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)

    app = FastAPI()
    app.include_router(view.build_data_router(), prefix="/api/plugins/portfolio")
    data = TestClient(app).get("/api/plugins/portfolio/overview").json()
    boards = {b["board"]: b for b in data["boards"]}
    assert boards["persistent"]["auto_dispose"] is False
