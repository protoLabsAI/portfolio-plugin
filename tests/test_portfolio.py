"""portfolio plugin tests (ADR 0055 P1).

The PM orchestration tools are pure composition over fleet × delegates ×
project_board — so the tests stub all three: no fleet, no A2A, no HTTP. They
assert the tools address the right remote board, dispatch over A2A with the
stored bearer, and read the structured board back.
"""

from __future__ import annotations

import json

import pytest

import portfolio


def _tool(name: str):
    return next(t for t in portfolio._tools() if t.name == name)


# ── portfolio_boards ─────────────────────────────────────────────────────────


def test_boards_lists_only_remote_members(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [
            {"name": "host", "host": True},
            {"name": "team-web", "remote": True, "url": "https://web.example", "running": True},
            {"name": "team-api", "remote": True, "url": "https://api.example", "running": False},
            {"name": "local-x", "port": 7890},  # a local member, not a remote → excluded
        ],
    )
    out = json.loads(_tool("portfolio_boards").invoke({}))
    assert {b["board"] for b in out} == {"team-web", "team-api"}
    assert next(b for b in out if b["board"] == "team-web")["reachable"] is True
    assert next(b for b in out if b["board"] == "team-api")["reachable"] is False


def test_boards_empty_message(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "host": True}])
    assert "No team boards yet" in _tool("portfolio_boards").invoke({})


# ── portfolio_dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_sends_over_a2a_with_the_stored_bearer(monkeypatch):
    from graph.fleet import supervisor
    from plugins.delegates import adapters

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "tok-web"}],
    )
    captured = {}

    async def fake_dispatch(d, query, *, timeout=None):
        captured.update(url=d.url, token=d.auth_token, scheme=d.auth_scheme, query=query, timeout=timeout)
        return "Created bd-7; state ready."

    monkeypatch.setattr(adapters.ADAPTERS["a2a"], "dispatch", fake_dispatch)

    async def _no_features(rec, state=""):  # empty board → dedup check passes cleanly
        return []

    monkeypatch.setattr(portfolio, "_fetch_board_features", _no_features)

    out = await _tool("portfolio_dispatch").ainvoke(
        {
            "board": "team-web",
            "title": "Add /healthz",
            "spec": "expose a readiness probe",
            "acceptance_criteria": "returns 200 when ready",
            "files_to_modify": "server.py",
        }
    )
    assert out == "Created bd-7; state ready."
    assert captured["url"] == "https://web.example/a2a"  # /a2a appended, trailing slash normalized
    assert captured["token"] == "tok-web" and captured["scheme"] == "bearer"
    # the instruction carries the spec + tells the team lead to use its board tools
    assert "Add /healthz" in captured["query"] and "board_create_feature" in captured["query"]
    assert "returns 200 when ready" in captured["query"] and "server.py" in captured["query"]


@pytest.mark.asyncio
async def test_dispatch_unknown_board(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    out = await _tool("portfolio_dispatch").ainvoke({"board": "nope", "title": "t", "spec": "s"})
    assert "no team board named 'nope'" in out


# ── portfolio_board_read ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_board_read_fetches_structured_features_with_bearer(monkeypatch):
    import httpx

    from graph.fleet import supervisor
    from security import policy

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example", "token": "tok-web"}],
    )
    monkeypatch.setattr(policy, "check_url", lambda _url: "")  # allow the read URL

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"features": [{"id": "bd-1", "title": "T", "state": "ready"}]}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, headers=None, params=None):
            seen.update(url=url, headers=headers, params=params)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    out = json.loads(await _tool("portfolio_board_read").ainvoke({"board": "team-web", "state": "ready"}))
    assert out == [{"id": "bd-1", "title": "T", "state": "ready"}]
    assert seen["url"] == "https://web.example/api/plugins/project_board/features"
    assert seen["headers"]["Authorization"] == "Bearer tok-web"
    assert seen["params"] == {"state": "ready"}


@pytest.mark.asyncio
async def test_board_read_unknown_board(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    out = await _tool("portfolio_board_read").ainvoke({"board": "nope"})
    assert "no team board named 'nope'" in out


# ── register ─────────────────────────────────────────────────────────────────
# (manifest shape + version coherence live in test_packaging.py, host-free)


def test_register_exposes_the_tools():
    seen = []

    class _Reg:
        def register_tool(self, t):
            seen.append(t.name)

    portfolio.register(_Reg())
    assert set(seen) == {
        "portfolio_boards",
        "portfolio_dispatch",
        "portfolio_cancel_feature",
        "portfolio_board_read",
        "portfolio_rollup",
        "portfolio_diff",
        "portfolio_watch",
        "portfolio_link",
        "portfolio_plan",
        "portfolio_autodispatch",
        "portfolio_spinup_team",
        "portfolio_archetypes",
        "portfolio_teams",
        "portfolio_teardown_team",
        "portfolio_autodispose",
    }


# ── portfolio_rollup (P2) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollup_projects_bounded_counts_and_only_blocked_critical(monkeypatch):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "team-web", "url": "https://web.example", "token": "t"},
            {"id": "r2", "name": "team-api", "url": "https://api.example", "token": "t"},
        ],
    )

    boards = {
        "team-web": [
            {"id": "w1", "title": "ready feat", "board_state": "ready"},
            {"id": "w2", "title": "blocked feat", "board_state": "in_progress", "blocked": True, "priority": 1},
            {"id": "w3", "title": "foundation", "board_state": "in_progress", "foundation": True, "priority": 2},
            {"id": "w4", "title": "done foundation", "board_state": "done", "foundation": True},
        ],
        "team-api": [{"id": "a1", "title": "x", "board_state": "backlog"}],
    }

    async def fake_fetch(rec, state=""):
        return boards[rec["name"]]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)

    out = {r["board"]: r for r in json.loads(await _tool("portfolio_rollup").ainvoke({}))}
    web = out["team-web"]
    assert web["total"] == 4
    assert web["counts"] == {"ready": 1, "in_progress": 2, "done": 1}
    assert web["blocked"] == [{"id": "w2", "title": "blocked feat", "priority": 1}]  # only the blocked one
    assert web["critical_path"] == [
        {"id": "w3", "title": "foundation", "state": "in_progress", "priority": 2}
    ]  # done foundation excluded, priority present
    # the rollup is BOUNDED — it carries counts + blocked/critical only, never the full feature list
    assert "spec" not in json.dumps(web) and "files_to_modify" not in json.dumps(web)
    assert out["team-api"]["counts"] == {"backlog": 1}


@pytest.mark.asyncio
async def test_rollup_priority_and_stuck(monkeypatch):
    """Priority on every blocked/critical entry, P0-first sort, stuck threshold,
    terminal exclusion, empty board. (ADR 0055 P2 — the rollup surfaces WHICH items
    matter, not just counts.)"""
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "board", "url": "https://b.example", "token": "t"}],
    )

    boards = {
        "board": [
            # P1 blocked — should come AFTER P0 in the sorted blocked list
            {"id": "b2", "title": "P1 blocked", "board_state": "in_progress", "blocked": True, "priority": 1},
            # P0 blocked — should come FIRST in the sorted blocked list
            {"id": "b1", "title": "P0 blocked", "board_state": "in_progress", "blocked": True, "priority": 0},
            # Same priority, different id — id breaks the tie
            {"id": "b3", "title": "P0 other", "board_state": "in_progress", "blocked": True, "priority": 0},
            # 1 attempt — NOT stuck (threshold is >= 2)
            {"id": "s1", "title": "one bounce", "board_state": "in_review", "attempts": [1]},
            # 2 attempts — stuck
            {"id": "s2", "title": "two bounces", "board_state": "in_review", "attempts": [1, 2], "priority": 1},
            # 3 attempts — stuck, highest-attempts-first
            {"id": "s3", "title": "three bounces", "board_state": "in_progress", "attempts": [1, 2, 3], "priority": 0},
            # done — must NOT appear in blocked / critical / stuck
            {"id": "d1", "title": "done blocked", "board_state": "done", "blocked": True, "priority": 0},
            # cancelled — must NOT appear in blocked / critical / stuck
            {"id": "d2", "title": "cancelled stuck", "board_state": "cancelled", "attempts": [1, 2]},
            # done foundation — must NOT appear in critical
            {"id": "d3", "title": "done found", "board_state": "done", "foundation": True},
        ],
    }

    async def fake_fetch(rec, state=""):
        return boards[rec["name"]]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)

    out = json.loads(await _tool("portfolio_rollup").ainvoke({}))[0]

    # Priority present on every blocked and critical_path entry
    for item in out["blocked"]:
        assert "priority" in item
    # P0-first sort: b1 (P0, id=b1) < b3 (P0, id=b3) < b2 (P1)
    assert [it["id"] for it in out["blocked"]] == ["b1", "b3", "b2"]
    # Terminal features excluded from blocked
    assert all(it["id"] not in {"d1", "d2", "d3"} for it in out["blocked"])

    # Stuck: s3 (3 attempts) first, then s2 (2 attempts); sorted attempts-desc, priority asc
    assert [it["id"] for it in out["stuck"]] == ["s3", "s2"]
    assert out["stuck"][0]["attempts"] == [1, 2, 3]
    assert out["stuck"][0]["priority"] == 0
    assert out["stuck"][1]["attempts"] == [1, 2]
    assert out["stuck"][1]["priority"] == 1
    # Terminal features excluded from stuck
    assert all(it["id"] not in {"d1", "d2", "d3"} for it in out["stuck"])
    # 1-attempt feature is NOT stuck
    assert all(it["id"] != "s1" for it in out["stuck"])

    # Empty board → stuck is empty
    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r2", "name": "empty", "url": "https://e.example", "token": "t"}],
    )
    boards["empty"] = []
    out2 = json.loads(await _tool("portfolio_rollup").ainvoke({}))[0]
    assert out2["stuck"] == [] and out2["blocked"] == [] and out2["critical_path"] == []


@pytest.mark.asyncio
async def test_rollup_filters_by_boards_arg(monkeypatch):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "team-web", "url": "https://web.example", "token": "t"},
            {"id": "r2", "name": "team-api", "url": "https://api.example", "token": "t"},
        ],
    )

    async def fake_fetch(rec, state=""):
        return []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    out = json.loads(await _tool("portfolio_rollup").ainvoke({"boards": "team-api"}))
    assert [r["board"] for r in out] == ["team-api"]


@pytest.mark.asyncio
async def test_rollup_tolerates_an_unreachable_board(monkeypatch):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "up", "url": "https://up.example", "token": "t"},
            {"id": "r2", "name": "down", "url": "https://down.example", "token": "t"},
        ],
    )

    async def fake_fetch(rec, state=""):
        if rec["name"] == "down":
            raise pf._BoardUnavailable("no project board exposed (project_board not enabled there)")
        return [{"id": "x", "board_state": "ready"}]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    out = {r["board"]: r for r in json.loads(await _tool("portfolio_rollup").ainvoke({}))}
    assert out["up"]["counts"] == {"ready": 1}
    assert (
        "error" in out["down"] and "no project board" in out["down"]["error"]
    )  # one bad board doesn't sink the rollup


@pytest.mark.asyncio
async def test_rollup_no_boards_message(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    assert "No team boards yet" in await _tool("portfolio_rollup").ainvoke({})


# ── portfolio_diff / portfolio_watch (P2 slice 2 — deltas) ───────────────────


def test_diff_boards_pure_projection():
    import portfolio as pf

    prev = {
        "f1": {"state": "in_progress", "blocked": False, "title": "merge me"},
        "f2": {"state": "in_progress", "blocked": True, "title": "stuck"},
        "f3": {"state": "ready", "blocked": False, "title": "steady"},
    }
    curr = {
        "f1": {"state": "done", "blocked": False, "title": "merge me"},  # → merged
        "f2": {"state": "in_progress", "blocked": False, "title": "stuck"},  # → unblocked
        "f3": {"state": "ready", "blocked": False, "title": "steady"},  # unchanged → no delta
        "f4": {
            "state": "in_progress",
            "blocked": True,
            "title": "fresh+blocked",
        },  # → new (not double-counted as blocked)
    }
    d = pf._diff_boards(prev, curr)
    assert d["merged"] == [{"id": "f1", "title": "merge me"}]
    assert d["unblocked"] == [{"id": "f2", "title": "stuck"}]
    assert d["new"] == [{"id": "f4", "title": "fresh+blocked", "state": "in_progress"}]
    assert "newly_blocked" not in d  # f4 is reported as new, not also as blocked
    assert pf._diff_boards(curr, curr) == {}  # no changes → empty


def _patch_board(monkeypatch, tmp_path, board_features: dict):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": n, "url": f"https://{n}.example", "token": "t"} for n in board_features],
    )
    state = {"features": board_features}

    async def fake_fetch(rec, fstate=""):
        return state["features"][rec["name"]]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    monkeypatch.setattr(pf, "_snapshot_path", lambda: tmp_path / "snap.json")
    return state


@pytest.mark.asyncio
async def test_diff_first_run_baselines_then_reports_changes(monkeypatch, tmp_path):
    state = _patch_board(
        monkeypatch,
        tmp_path,
        {"team-web": [{"id": "w1", "title": "feat", "board_state": "in_progress"}]},
    )

    # first run: records baseline, reports nothing
    out1 = await _tool("portfolio_diff").ainvoke({})
    assert "Baseline recorded" in out1
    assert (tmp_path / "snap.json").exists()

    # nothing changed → no-change message
    assert "No board changes" in await _tool("portfolio_diff").ainvoke({})

    # the feature merges → next diff reports it
    state["features"]["team-web"] = [{"id": "w1", "title": "feat", "board_state": "done"}]
    out3 = json.loads(await _tool("portfolio_diff").ainvoke({}))
    assert out3 == {"team-web": {"merged": [{"id": "w1", "title": "feat"}]}}

    # and it's consumed — a re-run sees no further change
    assert "No board changes" in await _tool("portfolio_diff").ainvoke({})


@pytest.mark.asyncio
async def test_watch_baselines_and_returns_schedule_guidance(monkeypatch, tmp_path):
    _patch_board(monkeypatch, tmp_path, {"team-web": [{"id": "w1", "title": "f", "board_state": "ready"}]})
    out = await _tool("portfolio_watch").ainvoke({"interval_min": 30})
    assert "Baseline captured for 1 board" in out
    assert 'when="*/30 * * * *"' in out and "schedule_task" in out
    assert (tmp_path / "snap.json").exists()  # baseline seeded


@pytest.mark.asyncio
async def test_diff_no_boards_message(monkeypatch, tmp_path):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    monkeypatch.setattr(pf, "_snapshot_path", lambda: tmp_path / "snap.json")
    assert "No team boards yet" in await _tool("portfolio_diff").ainvoke({})


# ── portfolio_link / portfolio_plan (P3 — cross-board dependency graph) ───────


def test_has_cycle_unit():
    import portfolio as pf

    edges = [
        {"from_board": "a", "from_feature": "1", "to_board": "b", "to_feature": "2"},
        {"from_board": "b", "from_feature": "2", "to_board": "a", "to_feature": "1"},
    ]
    assert pf._has_cycle(edges) is True
    assert pf._has_cycle(edges[:1]) is False  # a single edge is acyclic


def _patch_links(monkeypatch, tmp_path, boards=("team-web", "team-api")):
    from graph.fleet import supervisor
    import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": f"r-{n}", "name": n, "url": f"https://{n}.example", "token": "t"} for n in boards],
    )
    monkeypatch.setattr(pf, "_links_path", lambda: tmp_path / "links.json")


def test_link_add_dedup_self_unknown(monkeypatch, tmp_path):
    _patch_links(monkeypatch, tmp_path)
    link = _tool("portfolio_link")
    edge = {"from_board": "team-web", "from_feature": "w1", "to_board": "team-api", "to_feature": "a1"}

    out = json.loads(link.invoke(edge))
    assert out["from_board"] == "team-web" and out["to_feature"] == "a1" and out["id"].startswith("lnk-")
    assert "Already linked" in link.invoke(edge)  # dedup on the 4-tuple
    assert "can't depend on itself" in link.invoke(
        {"from_board": "team-web", "from_feature": "w1", "to_board": "team-web", "to_feature": "w1"}
    )
    assert "no team board named 'ghost'" in link.invoke(
        {"from_board": "team-web", "from_feature": "w1", "to_board": "ghost", "to_feature": "x"}
    )


def test_link_rejects_a_cycle(monkeypatch, tmp_path):
    _patch_links(monkeypatch, tmp_path)
    link = _tool("portfolio_link")
    link.invoke({"from_board": "team-web", "from_feature": "w1", "to_board": "team-api", "to_feature": "a1"})
    # the reverse edge would close a cycle (web:w1 → api:a1 → web:w1)
    out = link.invoke({"from_board": "team-api", "from_feature": "a1", "to_board": "team-web", "to_feature": "w1"})
    assert "cycle" in out


def test_link_remove(monkeypatch, tmp_path):
    _patch_links(monkeypatch, tmp_path)
    link = _tool("portfolio_link")
    eid = json.loads(
        link.invoke({"from_board": "team-web", "from_feature": "w1", "to_board": "team-api", "to_feature": "a1"})
    )["id"]
    assert "Removed" in link.invoke({"remove": eid})  # remove short-circuits before validation
    assert "No cross-board link" in link.invoke({"remove": "lnk-zzzzzzzz"})


def test_link_concurrent_writes_dont_drop(monkeypatch, tmp_path):
    """Two concurrent portfolio_link calls (the LLM firing both in one turn) BOTH persist —
    the file lock serializes the read-modify-write. Regression for the dropped-link race."""
    import threading

    _patch_links(monkeypatch, tmp_path)
    link = _tool("portfolio_link")
    barrier = threading.Barrier(2)

    def add(feat):
        barrier.wait()  # release both threads together to maximize the overlap
        link.invoke({"from_board": "team-web", "from_feature": feat, "to_board": "team-api", "to_feature": "a1"})

    threads = [threading.Thread(target=add, args=(f,)) for f in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    links = json.loads((tmp_path / "links.json").read_text())
    assert len(links) == 2  # neither append was clobbered
    assert {ln["from_feature"] for ln in links} == {"w1", "w2"}


@pytest.mark.asyncio
async def test_plan_satisfied_blocking_and_ready(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    pf._save_links(
        [
            {
                "id": "l1",
                "from_board": "team-web",
                "from_feature": "w1",
                "to_board": "team-api",
                "to_feature": "a1",
                "note": "",
            },
            {
                "id": "l2",
                "from_board": "team-web",
                "from_feature": "w2",
                "to_board": "team-api",
                "to_feature": "a2",
                "note": "",
            },
        ]
    )
    boards = {
        "team-web": [{"id": "w1", "board_state": "backlog"}, {"id": "w2", "board_state": "backlog"}],
        "team-api": [{"id": "a1", "board_state": "done"}, {"id": "a2", "board_state": "in_progress"}],
    }

    async def fake_fetch(rec, state=""):
        return boards[rec["name"]]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    plan = json.loads(await _tool("portfolio_plan").ainvoke({}))
    assert {ln["id"]: ln["status"] for ln in plan["links"]} == {"l1": "satisfied", "l2": "blocking"}
    assert plan["ready_to_dispatch"] == [{"board": "team-web", "feature": "w1", "state": "backlog"}]  # blocker done
    assert plan["blocked"][0]["feature"] == "w2"
    assert plan["blocked"][0]["blockers"][0]["feature"] == "a2"


@pytest.mark.asyncio
async def test_plan_unknown_and_dangling_fail_closed(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    pf._save_links(
        [
            {
                "id": "l1",
                "from_board": "team-web",
                "from_feature": "w1",
                "to_board": "team-api",
                "to_feature": "a1",
                "note": "",
            },
            {
                "id": "l2",
                "from_board": "team-web",
                "from_feature": "w2",
                "to_board": "team-web",
                "to_feature": "ghost",
                "note": "",
            },
        ]
    )

    async def fake_fetch(rec, state=""):
        if rec["name"] == "team-api":
            raise pf._BoardUnavailable("down")
        return [{"id": "w1", "board_state": "backlog"}, {"id": "w2", "board_state": "backlog"}]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    plan = json.loads(await _tool("portfolio_plan").ainvoke({}))
    assert {ln["id"]: ln["status"] for ln in plan["links"]} == {"l1": "unknown", "l2": "dangling"}
    assert plan["ready_to_dispatch"] == []  # fail-closed: never dispatch on an unknown/dangling blocker


@pytest.mark.asyncio
async def test_plan_empty(monkeypatch, tmp_path):
    import portfolio as pf

    monkeypatch.setattr(pf, "_links_path", lambda: tmp_path / "links.json")
    assert "No cross-board links yet" in await _tool("portfolio_plan").ainvoke({})


# ── portfolio_link planned-dispatch + portfolio_autodispatch (close the loop) ─


def test_link_planned_dispatch_carries_the_spec(monkeypatch, tmp_path):
    _patch_links(monkeypatch, tmp_path)
    out = json.loads(
        _tool("portfolio_link").invoke(
            {
                "from_board": "team-web",
                "from_feature": "render-v2",  # a planning label, not yet on the board
                "to_board": "team-api",
                "to_feature": "a1",
                "title": "Render users from /v2",
                "spec": "Wire the UI to /v2/users",
            }
        )
    )
    assert out["dispatched"] is False and out["title"] == "Render users from /v2"
    stored = json.loads((tmp_path / "links.json").read_text())[0]
    assert stored["spec"] == "Wire the UI to /v2/users" and stored["from_feature"] == "render-v2"


def _planned_link(tmp_path, **over):
    base = {
        "id": "l1",
        "from_board": "team-web",
        "from_feature": "render-v2",
        "to_board": "team-api",
        "to_feature": "a1",
        "note": "",
        "title": "Render users from /v2",
        "spec": "Wire the UI to /v2/users",
        "acceptance_criteria": "",
        "files_to_modify": "",
        "dispatched": False,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_autodispatch_creates_held_work_when_blocker_ships(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    pf._save_links([_planned_link(tmp_path)])

    # the blocker (team-api:a1) has shipped → done
    async def fake_fetch(rec, state=""):
        return [{"id": "a1", "board_state": "done"}] if rec["name"] == "team-api" else []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)

    dispatched = []

    async def fake_create(rec, title, spec, ac="", files=""):
        dispatched.append((rec["name"], title))
        return f"Created on {rec['name']}: {title}"

    monkeypatch.setattr(pf, "_a2a_create_feature", fake_create)

    out = json.loads(await _tool("portfolio_autodispatch").ainvoke({}))
    assert dispatched == [("team-web", "Render users from /v2")]  # held work created on its board
    assert out[0]["dispatched"] is True
    # idempotent: the link is now flagged → a second run dispatches nothing
    assert json.loads((tmp_path / "links.json").read_text())[0]["dispatched"] is True
    dispatched.clear()
    again = await _tool("portfolio_autodispatch").ainvoke({})
    assert dispatched == [] and "No pending" in again


@pytest.mark.asyncio
async def test_autodispatch_holds_while_blocked_and_dry_run(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    pf._save_links([_planned_link(tmp_path)])

    # blocker NOT done yet
    async def fake_fetch(rec, state=""):
        return [{"id": "a1", "board_state": "in_progress"}] if rec["name"] == "team-api" else []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)

    called = []
    monkeypatch.setattr(pf, "_a2a_create_feature", lambda *a, **k: called.append(1))

    assert "still blocked" in await _tool("portfolio_autodispatch").ainvoke({})
    assert called == []  # held — nothing dispatched while the blocker is open

    # now it ships; dry_run previews without dispatching or flagging
    async def fake_done(rec, state=""):
        return [{"id": "a1", "board_state": "done"}] if rec["name"] == "team-api" else []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_done)
    preview = json.loads(await _tool("portfolio_autodispatch").ainvoke({"dry_run": True}))
    assert preview["would_dispatch"][0]["board"] == "team-web"
    assert called == []  # dry run dispatched nothing
    assert json.loads((tmp_path / "links.json").read_text())[0]["dispatched"] is False  # not flagged


@pytest.mark.asyncio
async def test_autodispatch_ignores_advisory_only_links(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    # an advisory link (no spec) — autodispatch must never touch it
    pf._save_links(
        [
            {
                "id": "l1",
                "from_board": "team-web",
                "from_feature": "w1",
                "to_board": "team-api",
                "to_feature": "a1",
                "note": "",
            }
        ]
    )
    assert "No pending planned-dispatch links" in await _tool("portfolio_autodispatch").ainvoke({})


@pytest.mark.asyncio
async def test_plan_excludes_dispatched_planned_links(monkeypatch, tmp_path):
    import portfolio as pf

    _patch_links(monkeypatch, tmp_path)
    pf._save_links([_planned_link(tmp_path, dispatched=True)])

    async def fake_fetch(rec, state=""):
        return [{"id": "a1", "board_state": "done"}] if rec["name"] == "team-api" else []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    plan = json.loads(await _tool("portfolio_plan").ainvoke({}))
    assert plan["links"][0]["dispatched"] is True
    assert plan["ready_to_dispatch"] == [] and plan["blocked"] == []  # already dispatched → off the work lists


# ── portfolio_dispatch dedup (#25) ────────────────────────────────────────────


def test_open_duplicate_matches_normalized_title_on_open_lanes_only():
    feats = [
        {"id": "bd-1", "title": "Assess repo", "board_state": "done"},  # terminal → not a dup
        {"id": "bd-2", "title": "  ASSESS   repo ", "board_state": "in_progress"},  # open, normalizes-equal
    ]
    dup = portfolio._open_duplicate(feats, "assess repo")
    assert dup is not None and dup["id"] == "bd-2"
    # a title only present in a terminal lane is NOT a live duplicate
    assert portfolio._open_duplicate([feats[0]], "Assess repo") is None
    # no match
    assert portfolio._open_duplicate(feats, "Different task") is None


@pytest.mark.asyncio
async def test_dispatch_refuses_a_same_title_open_feature(monkeypatch):
    from graph.fleet import supervisor
    from plugins.delegates import adapters

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "t"}],
    )

    async def _feats(rec, state=""):
        return [{"id": "bd-3", "title": "Assess repo", "board_state": "in_progress"}]

    monkeypatch.setattr(portfolio, "_fetch_board_features", _feats)
    dispatched = {"n": 0}

    async def _should_not_run(d, query, *, timeout=None):
        dispatched["n"] += 1
        return "created"

    monkeypatch.setattr(adapters.ADAPTERS["a2a"], "dispatch", _should_not_run)

    out = await _tool("portfolio_dispatch").ainvoke({"board": "team-web", "title": "assess repo", "spec": "s"})
    assert "already open" in out and "bd-3" in out and "in_progress" in out
    assert dispatched["n"] == 0  # never dispatched the duplicate


@pytest.mark.asyncio
async def test_dispatch_force_overrides_dedup(monkeypatch):
    from graph.fleet import supervisor
    from plugins.delegates import adapters

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "t"}],
    )

    async def _feats(rec, state=""):
        return [{"id": "bd-3", "title": "Assess repo", "board_state": "ready"}]

    monkeypatch.setattr(portfolio, "_fetch_board_features", _feats)

    async def _dispatch(d, query, *, timeout=None):
        return "Created bd-9; state ready."

    monkeypatch.setattr(adapters.ADAPTERS["a2a"], "dispatch", _dispatch)
    out = await _tool("portfolio_dispatch").ainvoke(
        {"board": "team-web", "title": "Assess repo", "spec": "s", "force": True}
    )
    assert out == "Created bd-9; state ready."


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_the_board_cant_be_read(monkeypatch):
    """An unreadable board must never block a dispatch — better a possible dup than a
    PM that can't dispatch at all."""
    from graph.fleet import supervisor
    from plugins.delegates import adapters
    from portfolio import _BoardUnavailable

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "t"}],
    )

    async def _boom(rec, state=""):
        raise _BoardUnavailable("no board exposed")

    monkeypatch.setattr(portfolio, "_fetch_board_features", _boom)

    async def _dispatch(d, query, *, timeout=None):
        return "Created bd-1; state ready."

    monkeypatch.setattr(adapters.ADAPTERS["a2a"], "dispatch", _dispatch)
    out = await _tool("portfolio_dispatch").ainvoke({"board": "team-web", "title": "anything", "spec": "s"})
    assert out == "Created bd-1; state ready."


# ── portfolio_cancel_feature (#27) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_feature_posts_to_the_board_with_the_bearer(monkeypatch):
    import httpx

    from graph.fleet import supervisor
    from security import policy

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "tok-web"}],
    )
    monkeypatch.setattr(policy, "check_url", lambda _u: "")  # allow
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "bd-3", "board_state": "cancelled"}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            seen.update(url=url, headers=headers, body=json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    out = json.loads(
        await _tool("portfolio_cancel_feature").ainvoke({"board": "team-web", "feature_id": "bd-3", "reason": "dup"})
    )
    assert out == {"id": "bd-3", "board_state": "cancelled"}
    assert seen["url"] == "https://web.example/api/plugins/project_board/features/bd-3/cancel"
    assert seen["headers"]["Authorization"] == "Bearer tok-web"
    assert seen["body"] == {"reason": "dup"}


@pytest.mark.asyncio
async def test_cancel_feature_unknown_board(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    out = await _tool("portfolio_cancel_feature").ainvoke({"board": "nope", "feature_id": "bd-1"})
    assert "no team board named 'nope'" in out


@pytest.mark.asyncio
async def test_cancel_feature_missing_id_surfaces_a_clean_error(monkeypatch):
    import httpx

    from graph.fleet import supervisor
    from security import policy

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example", "token": "t"}],
    )
    monkeypatch.setattr(policy, "check_url", lambda _u: "")

    class _Resp:
        status_code = 404
        text = "not found"

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await _tool("portfolio_cancel_feature").ainvoke({"board": "team-web", "feature_id": "ghost"})
    assert "Error cancelling 'ghost' on 'team-web'" in out and "not found" in out
