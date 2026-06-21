"""portfolio team-spawning tests (v0.6) — the ephemeral-team lifecycle.

portfolio_spinup_team / teardown_team / autodispose are composition over the SAME
in-process primitives team-up.sh drives by hand — graph.workspaces.manager (clone a
team config into a scoped workspace) + graph.fleet.supervisor (start/stop/register).
So the tests stub both: no real workspace, no real server, no HTTP. They assert the
tool clones the template, BINDS the repo into the cloned config (sentinels filled),
starts + registers the team, records it, and that teardown/autodispose only ever touch
portfolio-spawned teams — never a hand-registered remote, never an empty board.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import portfolio


def _tool(name: str, cfg: dict | None = None):
    return next(t for t in portfolio._tools(cfg or {}) if t.name == name)


async def _ready_true(base, timeout=40.0):
    return True


async def _all_done(rec, state=""):
    return [{"id": "1", "board_state": "done"}]


# ── fixtures ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_teams(tmp_path, monkeypatch):
    """Point the spawned-teams registry at a tmp file so tests don't collide / leak."""
    monkeypatch.setattr(portfolio, "_teams_path", lambda: tmp_path / "portfolio_teams.json")


@pytest.fixture
def template(tmp_path):
    """A base team config carrying the per-spawn sentinels (the shape team-up.sh fills)."""
    d = tmp_path / "team-template"
    d.mkdir()
    cfg = d / "langgraph-config.yaml"
    cfg.write_text(
        'identity:\n  name: {{TEAM_NAME}}\nproject_board:\n  workdir: {{REPO}}\n  local_gate_cmd: "{{GATE}}"\n'
    )
    (d / "secrets.yaml").write_text("# secrets\n")
    return cfg


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """A fake fleet: manager.create copies the template into a tmp workspace; start/stop/
    add_remote/remove_remote/list_remotes operate on an in-memory remotes dict."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    state: dict = {"remotes": {}, "started": [], "stopped": [], "removed": []}

    def fake_create(name, *, from_config=None, port=None, **k):
        ws = tmp_path / "ws" / name
        ws.mkdir(parents=True)
        src = Path(from_config)
        src = src if src.is_file() else src / "langgraph-config.yaml"
        shutil.copyfile(src, ws / "langgraph-config.yaml")
        return {"id": f"{name}-abcd", "name": name, "port": port or 7874, "path": str(ws)}

    def fake_start(ident):
        state["started"].append(ident)
        return {"id": ident, "port": 7874, "running": True}

    def fake_add_remote(name, url, token=""):
        state["remotes"][name] = {"id": name, "name": name, "url": url.rstrip("/"), "token": token}
        return {"name": name}

    def fake_remove_remote(ident):
        state["removed"].append(ident)
        state["remotes"].pop(ident, None)
        return {"name": ident, "removed": ["remote"]}

    def fake_stop(ident, **k):
        state["stopped"].append(ident)
        return {"name": ident, "stopped": True}

    def fake_remove(ident, *, purge=False):
        return {"name": ident, "removed": ["workspace", "data"] if purge else ["workspace"]}

    monkeypatch.setattr(manager, "create", fake_create)
    monkeypatch.setattr(manager, "remove", fake_remove)
    monkeypatch.setattr(supervisor, "start", fake_start)
    monkeypatch.setattr(supervisor, "stop", fake_stop)
    monkeypatch.setattr(supervisor, "add_remote", fake_add_remote)
    monkeypatch.setattr(supervisor, "remove_remote", fake_remove_remote)
    monkeypatch.setattr(supervisor, "list_remotes", lambda: list(state["remotes"].values()))
    monkeypatch.setattr(portfolio, "_await_ready", _ready_true)
    monkeypatch.setattr(portfolio, "_beads_init", lambda repo: None)
    return state


# ── portfolio_spinup_team ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spinup_clones_binds_starts_registers(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke(
            {"name": "alpha", "repo": str(repo), "template": str(template), "gate": "ruff check ."}
        )
    )
    assert out["team"] == "alpha"
    assert out["a2a"] == "http://127.0.0.1:7874/a2a"
    assert out["ready"] is True
    # registered as a fleet remote + recorded in the spawned-teams registry
    assert "alpha" in fleet["remotes"]
    assert portfolio._team_by_name("alpha")["repo"] == out["repo"]
    # the repo was BOUND into the cloned config — every sentinel filled
    bound = (tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text()
    assert "{{" not in bound
    assert out["repo"] in bound and "name: alpha" in bound and "ruff check ." in bound


@pytest.mark.asyncio
async def test_spinup_uses_config_team_template(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tool = _tool("portfolio_spinup_team", {"team_template": str(template)})
    out = json.loads(await tool.ainvoke({"name": "beta", "repo": str(repo)}))
    assert out["team"] == "beta" and "beta" in fleet["remotes"]


@pytest.mark.asyncio
async def test_spinup_requires_a_template(fleet, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = await _tool("portfolio_spinup_team").ainvoke({"name": "x", "repo": str(repo)})
    assert "no team template" in out.lower()


@pytest.mark.asyncio
async def test_spinup_rejects_a_duplicate_name(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fleet["remotes"]["dup"] = {"id": "dup", "name": "dup", "url": "http://x", "token": ""}
    out = await _tool("portfolio_spinup_team").ainvoke({"name": "dup", "repo": str(repo), "template": str(template)})
    assert "already exists" in out


@pytest.mark.asyncio
async def test_spinup_rejects_a_missing_repo(fleet, template, tmp_path):
    out = await _tool("portfolio_spinup_team").ainvoke(
        {"name": "x", "repo": str(tmp_path / "nope"), "template": str(template)}
    )
    assert "repo path not found" in out


@pytest.mark.asyncio
async def test_spinup_rolls_back_on_start_failure(fleet, template, tmp_path, monkeypatch):
    from graph.fleet import supervisor
    from graph.workspaces import manager

    removed: list = []
    monkeypatch.setattr(manager, "remove", lambda ident, *, purge=False: removed.append(ident) or {"removed": []})

    def boom(ident):
        raise RuntimeError("boot failed")

    monkeypatch.setattr(supervisor, "start", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = await _tool("portfolio_spinup_team").ainvoke({"name": "gamma", "repo": str(repo), "template": str(template)})
    assert "Error starting team" in out
    assert removed == ["gamma-abcd"]  # the workspace was purged
    assert portfolio._team_by_name("gamma") is None  # never recorded


# ── portfolio_teardown_team ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teardown_disposes_a_spawned_team(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _tool("portfolio_spinup_team").ainvoke({"name": "alpha", "repo": str(repo), "template": str(template)})
    out = json.loads(await _tool("portfolio_teardown_team").ainvoke({"name": "alpha"}))
    assert out["stopped"] and out["purged"] and out["unregistered"]
    assert "alpha-abcd" in fleet["stopped"]
    assert "alpha" not in fleet["remotes"]
    assert portfolio._team_by_name("alpha") is None


@pytest.mark.asyncio
async def test_teardown_rejects_a_non_spawned_remote(fleet):
    out = await _tool("portfolio_teardown_team").ainvoke({"name": "nope"})
    assert "not a portfolio-spawned team" in out


# ── portfolio_autodispose ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autodispose_disposes_only_drained_boards(fleet, monkeypatch):
    boards = {
        "drained": [{"id": "1", "board_state": "done"}, {"id": "2", "board_state": "done"}],
        "active": [{"id": "1", "board_state": "done"}, {"id": "2", "board_state": "in_progress"}],
        "empty": [],  # spawned but never given work — must NOT be disposed
    }
    for n in boards:
        fleet["remotes"][n] = {"id": n, "name": n, "url": f"http://127.0.0.1/{n}", "token": ""}
        portfolio._record_team(
            {"name": n, "id": f"{n}-id", "port": 7000, "repo": "/r", "auto_dispose": True, "spawned_at": "t"}
        )

    async def fake_fetch(rec, state=""):
        return boards[rec["name"]]

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)
    out = json.loads(await _tool("portfolio_autodispose").ainvoke({}))
    assert [d["team"] for d in out["disposed"]] == ["drained"]
    assert portfolio._team_by_name("drained") is None
    assert portfolio._team_by_name("active") is not None
    assert portfolio._team_by_name("empty") is not None


@pytest.mark.asyncio
async def test_autodispose_dry_run_previews_without_disposing(fleet, monkeypatch):
    fleet["remotes"]["d"] = {"id": "d", "name": "d", "url": "http://x", "token": ""}
    portfolio._record_team(
        {"name": "d", "id": "d-id", "port": 1, "repo": "/r", "auto_dispose": True, "spawned_at": "t"}
    )
    monkeypatch.setattr(portfolio, "_fetch_board_features", _all_done)
    out = json.loads(await _tool("portfolio_autodispose").ainvoke({"dry_run": True}))
    assert out["would_dispose"] == ["d"]
    assert portfolio._team_by_name("d") is not None  # untouched


@pytest.mark.asyncio
async def test_autodispose_ignores_manual_teams(fleet):
    portfolio._record_team(
        {"name": "manual", "id": "m", "port": 1, "repo": "/r", "auto_dispose": False, "spawned_at": "t"}
    )
    out = await _tool("portfolio_autodispose").ainvoke({})
    assert "No auto-dispose teams" in out


# ── portfolio_teams + bindings ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_portfolio_teams_reports_board_status(fleet, monkeypatch):
    fleet["remotes"]["alpha"] = {"id": "alpha", "name": "alpha", "url": "http://x", "token": ""}
    portfolio._record_team(
        {"name": "alpha", "id": "a", "port": 7874, "repo": "/r", "auto_dispose": True, "spawned_at": "t"}
    )

    async def fake_fetch(rec, state=""):
        return [{"id": "1", "board_state": "done"}, {"id": "2", "board_state": "in_progress"}]

    monkeypatch.setattr(portfolio, "_fetch_board_features", fake_fetch)
    out = json.loads(await _tool("portfolio_teams").ainvoke({}))
    assert out[0]["team"] == "alpha"
    assert out[0]["board"] == {"total": 2, "done": 1, "active": 1, "drained": False}


@pytest.mark.asyncio
async def test_portfolio_teams_empty(fleet):
    out = await _tool("portfolio_teams").ainvoke({})
    assert "No spawned teams" in out


def test_apply_team_bindings_fills_every_sentinel(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('repo: {{REPO}}\nname: {{TEAM_NAME}}\ngate: "{{GATE}}"\n')
    portfolio._apply_team_bindings(p, "/abs/repo", "alpha", "ruff check .")
    t = p.read_text()
    assert "{{" not in t
    assert "/abs/repo" in t and "name: alpha" in t and "ruff check ." in t


def test_apply_team_bindings_keeps_baked_repo_when_none_given(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("repo: {{REPO}}\nname: {{TEAM_NAME}}\n")
    portfolio._apply_team_bindings(p, "", "beta", "")
    t = p.read_text()
    assert "{{REPO}}" in t  # a prebuilt template's baked repo is left untouched
    assert "name: beta" in t
