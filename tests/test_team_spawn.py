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
    """A fake fleet: manager.create copies the template into a tmp workspace + records it;
    start marks it running so it joins status() as a LOCAL member (how a spawned team
    becomes dispatchable — no add_remote); list_remotes is for external members only."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    state: dict = {"remotes": {}, "workspaces": {}, "running": set(), "started": [], "stopped": [], "removed": []}

    def fake_create(name, *, from_config=None, port=None, **k):
        ws = tmp_path / "ws" / name
        ws.mkdir(parents=True)
        src = Path(from_config)
        src = src if src.is_file() else src / "langgraph-config.yaml"
        shutil.copyfile(src, ws / "langgraph-config.yaml")
        rec = {"id": f"{name}-abcd", "name": name, "port": port or 7874, "path": str(ws)}
        state["workspaces"][rec["id"]] = rec
        return rec

    def fake_start(ident):
        state["started"].append(ident)
        state["running"].add(ident)
        ws = state["workspaces"].get(ident, {})
        return {"id": ident, "port": ws.get("port", 7874), "running": True}

    def fake_stop(ident, **k):
        state["stopped"].append(ident)
        state["running"].discard(ident)
        return {"name": ident, "stopped": True}

    def fake_remove(ident, *, purge=False):
        state["removed"].append(ident)
        state["workspaces"].pop(ident, None)
        state["running"].discard(ident)
        return {"name": ident, "removed": ["workspace", "data"] if purge else ["workspace"]}

    def fake_remove_remote(ident):
        if ident not in state["remotes"]:
            raise Exception(f"no remote member {ident!r}")  # faithful: real supervisor raises FleetError
        state["remotes"].pop(ident, None)
        return {"name": ident, "removed": ["remote"]}

    def fake_status():
        out = [{"name": "host", "host": True}]
        for wid, ws in state["workspaces"].items():
            if wid in state["running"]:
                out.append({"name": ws["name"], "id": wid, "port": ws["port"], "running": True})
        for r in state["remotes"].values():
            out.append({"name": r["name"], "id": r["id"], "remote": True, "url": r["url"], "running": True})
        return out

    monkeypatch.setattr(manager, "create", fake_create)
    monkeypatch.setattr(manager, "remove", fake_remove)
    monkeypatch.setattr(supervisor, "start", fake_start)
    monkeypatch.setattr(supervisor, "stop", fake_stop)
    monkeypatch.setattr(supervisor, "remove_remote", fake_remove_remote)
    monkeypatch.setattr(supervisor, "status", fake_status)
    monkeypatch.setattr(supervisor, "list_remotes", lambda: list(state["remotes"].values()))
    monkeypatch.setattr(portfolio, "_await_ready", _ready_true)
    monkeypatch.setattr(portfolio, "_beads_init", lambda repo: None)
    monkeypatch.setattr(portfolio, "_freshen_repo", lambda repo: "stubbed")  # no real git in spinup tests
    monkeypatch.setattr(portfolio, "_host_plugins_dir", lambda: str(tmp_path / "host-plugins"))

    async def fake_a2a_send(base, instruction, *, token="", timeout=240):
        state["onboard_sent"].append({"base": base, "instruction": instruction})
        return "Readiness: PROTO.md FIXED, gate PASS, board empty."

    monkeypatch.setattr(portfolio, "_a2a_send", fake_a2a_send)
    state["onboard_sent"] = []
    state["host_plugins"] = str(tmp_path / "host-plugins")
    return state


# ── portfolio_spinup_team ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spinup_clones_binds_starts_registers(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # a git repo, so repo-prep can write .git/info/exclude
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke(
            {"name": "alpha", "repo": str(repo), "template": str(template), "gate": "ruff check ."}
        )
    )
    assert out["team"] == "alpha"
    assert out["a2a"] == "http://127.0.0.1:7874/a2a"
    assert out["ready"] is True
    # joined the fleet as a LOCAL member (no add_remote) — resolvable + dispatchable by name
    assert "alpha" not in fleet["remotes"]
    assert portfolio._remote_by_name("alpha") == {
        "name": "alpha",
        "id": "alpha-abcd",
        "url": "http://127.0.0.1:7874",
        "token": "",
    }
    # recorded in the spawned-teams registry
    assert portfolio._team_by_name("alpha")["repo"] == out["repo"]
    # repo-prep excluded the coder's per-session .proto scratch (so its PRs ship clean)
    assert ".proto/memory/" in (repo / ".git" / "info" / "exclude").read_text()
    # the repo was BOUND into the cloned config — every sentinel filled
    bound = (tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text()
    assert "{{" not in bound
    assert out["repo"] in bound and "name: alpha" in bound and "ruff check ." in bound
    # the team was sent a one-time onboarding instruction (over A2A) + summary surfaced
    assert len(fleet["onboard_sent"]) == 1
    assert "onboard-project" in fleet["onboard_sent"][0]["instruction"]
    assert out["onboarding"].startswith("Readiness:")


@pytest.mark.asyncio
async def test_spinup_auto_detects_the_gate(fleet, template, tmp_path):
    """No gate passed → spinup infers the repo's real check command and binds it."""
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke({"name": "alpha", "repo": str(repo), "template": str(template)})
    )
    assert out["gate"] == "ruff check . && ruff format --check ."
    bound = (tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text()
    assert "ruff check . && ruff format --check ." in bound


@pytest.mark.asyncio
async def test_spinup_explicit_gate_beats_autodetect(fleet, template, tmp_path):
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke(
            {"name": "alpha", "repo": str(repo), "template": str(template), "gate": "make check"}
        )
    )
    assert out["gate"] == "make check"


@pytest.mark.asyncio
async def test_spinup_onboard_false_skips_the_kick(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke(
            {"name": "alpha", "repo": str(repo), "template": str(template), "onboard": False}
        )
    )
    assert out["onboarding"] == "skipped"
    assert fleet["onboard_sent"] == []  # no A2A onboarding message sent


def test_detect_gate_by_stack(tmp_path):
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("")
    assert portfolio._detect_gate(str(py)).startswith("ruff check")

    vp = tmp_path / "vp"
    vp.mkdir()
    (vp / "package.json").write_text('{"devDependencies":{"vitepress":"1.0"}}')
    assert portfolio._detect_gate(str(vp)) == "npm ci && npm run docs:build"

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text('{"name":"x"}')
    assert portfolio._detect_gate(str(node)) == "npm ci && npm test"

    bare = tmp_path / "bare"
    bare.mkdir()
    assert portfolio._detect_gate(str(bare)) == ""  # unrecognized → empty (onboarding may set one)


@pytest.mark.asyncio
async def test_spinup_defaults_plugins_dir_to_host(fleet, template, tmp_path):
    """External plugins (project_board/github) only load with a discovery root — the
    spawned team defaults plugins.dir to the PM host's own plugins dir."""
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    await _tool("portfolio_spinup_team").ainvoke({"name": "alpha", "repo": str(repo), "template": str(template)})
    doc = yaml.safe_load((tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text())
    assert doc["plugins"]["dir"] == fleet["host_plugins"]


@pytest.mark.asyncio
async def test_spinup_honors_explicit_plugins_dir(fleet, template, tmp_path):
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    await _tool("portfolio_spinup_team").ainvoke(
        {"name": "alpha", "repo": str(repo), "template": str(template), "plugins_dir": "/custom/plugins"}
    )
    doc = yaml.safe_load((tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text())
    assert doc["plugins"]["dir"] == "/custom/plugins"


def test_ensure_plugins_dir_sets_when_absent(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text("plugins:\n  enabled: [delegates]\n")
    portfolio._ensure_plugins_dir(p, "/host/plugins")
    doc = yaml.safe_load(p.read_text())
    assert doc["plugins"]["dir"] == "/host/plugins"
    assert doc["plugins"]["enabled"] == ["delegates"]


def test_ensure_plugins_dir_respects_template_choice(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text("plugins:\n  enabled: [delegates]\n  dir: /already/here\n")
    portfolio._ensure_plugins_dir(p, "/should/not/win")
    doc = yaml.safe_load(p.read_text())
    assert doc["plugins"]["dir"] == "/already/here"


@pytest.mark.asyncio
async def test_spinup_uses_config_team_template(fleet, template, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tool = _tool("portfolio_spinup_team", {"team_template": str(template)})
    out = json.loads(await tool.ainvoke({"name": "beta", "repo": str(repo)}))
    assert out["team"] == "beta" and portfolio._remote_by_name("beta") is not None


@pytest.mark.asyncio
async def test_spinup_defaults_to_the_shipped_template(fleet, tmp_path):
    """With no template arg / config, spinup falls back to the plugin's shipped example."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out = json.loads(await _tool("portfolio_spinup_team").ainvoke({"name": "x", "repo": str(repo)}))
    assert out["team"] == "x"
    assert portfolio._default_template().endswith("examples/team-template")


@pytest.mark.asyncio
async def test_spinup_errors_when_no_template_at_all(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "_default_template", lambda: "")  # simulate no shipped default
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
    assert portfolio._remote_by_name("alpha") is not None  # live local member before teardown
    out = json.loads(await _tool("portfolio_teardown_team").ainvoke({"name": "alpha"}))
    # a local team was never a remote, so disposal = stopped + purged (no remote to unregister)
    assert out["stopped"] and out["purged"]
    assert "unregistered" not in out
    assert "alpha-abcd" in fleet["stopped"]
    assert portfolio._remote_by_name("alpha") is None  # gone from the fleet
    assert portfolio._team_by_name("alpha") is None


@pytest.mark.asyncio
async def test_teardown_rejects_a_non_spawned_remote(fleet):
    out = await _tool("portfolio_teardown_team").ainvoke({"name": "nope"})
    assert "not a portfolio-spawned team" in out


def test_remote_by_name_prefers_remote_then_local(fleet, monkeypatch):
    """A remote (with a token) wins a name clash; otherwise a local fleet member resolves
    on 127.0.0.1:<port> with no token — how a spawned team is addressed without add_remote."""
    from graph.fleet import supervisor

    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [
            {"name": "host", "host": True},
            {"name": "local-team", "id": "local-team-1", "port": 7880, "running": True},
            {"name": "ext", "id": "ext-1", "remote": True, "url": "https://ext.example", "running": True},
        ],
    )
    monkeypatch.setattr(
        supervisor, "list_remotes", lambda: [{"id": "ext-1", "name": "ext", "url": "https://ext.example", "token": "t"}]
    )
    assert portfolio._remote_by_name("ext")["token"] == "t"  # remote wins (carries auth)
    assert portfolio._remote_by_name("local-team") == {
        "name": "local-team",
        "id": "local-team-1",
        "url": "http://127.0.0.1:7880",
        "token": "",
    }
    assert portfolio._remote_by_name("host") is None  # never the PM itself
    assert portfolio._remote_by_name("absent") is None


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


@pytest.mark.asyncio
async def test_autodispose_prunes_a_dead_team(fleet, monkeypatch):
    """A spawned team whose process is GONE (e.g. a host restart killed it) but whose
    workspace lingers as an unreachable zombie board is disposed — not left up forever."""
    from graph.fleet import supervisor

    fleet["remotes"]["dead"] = {"id": "dead", "name": "dead", "url": "http://127.0.0.1:9999", "token": ""}
    portfolio._record_team(
        {"name": "dead", "id": "dead-id", "port": 9999, "repo": "/r", "auto_dispose": True, "spawned_at": "t"}
    )

    async def unreachable(rec, state=""):
        raise portfolio._BoardUnavailable("connection refused")

    monkeypatch.setattr(portfolio, "_fetch_board_features", unreachable)
    monkeypatch.setattr(supervisor, "is_running", lambda ident: False)  # the process is dead
    out = json.loads(await _tool("portfolio_autodispose").ainvoke({}))
    assert [d["team"] for d in out["disposed"]] == ["dead"]
    assert out["disposed"][0]["reason"] == "dead (process gone)"
    assert portfolio._team_by_name("dead") is None  # the zombie entry is gone


@pytest.mark.asyncio
async def test_autodispose_keeps_a_live_but_unreachable_team(fleet, monkeypatch):
    """A still-running team with a transient board hiccup is LEFT UP (not mistaken for dead)."""
    from graph.fleet import supervisor

    fleet["remotes"]["blip"] = {"id": "blip", "name": "blip", "url": "http://x", "token": ""}
    portfolio._record_team(
        {"name": "blip", "id": "blip-id", "port": 1, "repo": "/r", "auto_dispose": True, "spawned_at": "t"}
    )

    async def unreachable(rec, state=""):
        raise portfolio._BoardUnavailable("temporary 503")

    monkeypatch.setattr(portfolio, "_fetch_board_features", unreachable)
    monkeypatch.setattr(supervisor, "is_running", lambda ident: True)  # still alive
    out = json.loads(await _tool("portfolio_autodispose").ainvoke({}))
    assert out["disposed"] == []
    assert portfolio._team_by_name("blip") is not None  # left up


def test_record_team_is_locked_during_rmw(fleet, tmp_path, monkeypatch):
    """_record_team takes the registry file lock so concurrent spinups don't drop entries."""
    held = {}
    real_lock = portfolio._file_lock

    def tracking_lock(path):
        held["path"] = str(path)
        return real_lock(path)

    monkeypatch.setattr(portfolio, "_file_lock", tracking_lock)
    portfolio._record_team({"name": "x", "id": "x", "auto_dispose": True})
    assert held["path"].endswith("portfolio_teams.json")  # the lock wraps the teams RMW


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


# ── _exclude_proto_scratch (#49) — ignore proto SESSION scratch, keep .proto/evolve skills ──


def _excludes(repo):
    return {
        ln.strip()
        for ln in (repo / ".git" / "info" / "exclude").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    }


def test_proto_exclude_ignores_only_session_scratch(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    portfolio._exclude_proto_scratch(str(repo))
    patterns = _excludes(repo)
    # the per-session scratch is excluded (via .git/info/exclude — worktree-shared, not committed)
    assert ".proto/memory/" in patterns
    assert ".proto/session-notes.md" in patterns
    assert ".proto/repo-map-cache.json" in patterns
    # but NEVER the whole dir — protoCLI skills under .proto/evolve must stay versioned
    assert ".proto/" not in patterns and ".proto" not in patterns
    assert not any(p.startswith(".proto/evolve") for p in patterns)


def test_proto_exclude_leaves_committed_gitignore_untouched(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("node_modules/\n*.db\n")
    portfolio._exclude_proto_scratch(str(repo))
    # no churn in the repo's tracked .gitignore — the ignore lives in .git/info/exclude
    assert (repo / ".gitignore").read_text() == "node_modules/\n*.db\n"
    assert ".proto/memory/" in _excludes(repo)


def test_proto_exclude_skips_a_non_git_dir(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()  # no .git
    portfolio._exclude_proto_scratch(str(repo))  # no-op, no error
    assert not (repo / ".git").exists()


def test_proto_exclude_is_idempotent(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    portfolio._exclude_proto_scratch(str(repo))
    once = (repo / ".git" / "info" / "exclude").read_text()
    portfolio._exclude_proto_scratch(str(repo))
    assert (repo / ".git" / "info" / "exclude").read_text() == once  # marker guards a re-add


# ── branch freshness (_freshen_repo) + prebuilt archetypes ───────────────────────


def _git(repo, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_freshen_repo_fast_forwards_a_stale_clone(tmp_path):
    """A clone behind its origin is fast-forwarded to current before the team works on it."""
    import subprocess

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        import pytest

        pytest.skip("git not available")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "c1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    # advance origin one commit → the clone is now behind
    (origin / "a.txt").write_text("2")
    _git(origin, "commit", "-qam", "c2")
    before = _git(clone, "rev-parse", "HEAD").stdout.strip()
    status = portfolio._freshen_repo(str(clone))
    after = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert "up to date" in status and "fast-forwarded" in status
    assert after != before  # the clone moved forward to origin's HEAD


def test_freshen_repo_skips_a_repo_with_no_remote(tmp_path):
    import subprocess

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        import pytest

        pytest.skip("git not available")
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    assert "no remote" in portfolio._freshen_repo(str(r))


def test_freshen_repo_non_git_dir(tmp_path):
    assert "not a git repo" in portfolio._freshen_repo(str(tmp_path))


def test_archetypes_parses_dict_and_string_forms():
    cfg = {"team_archetypes": {"pc": {"repo": "/dev/pc", "gate": "build"}, "pa": "/dev/pa"}}
    arch = portfolio._archetypes(cfg)
    assert arch["pc"] == {"repo": "/dev/pc", "gate": "build"}
    assert arch["pa"] == {"repo": "/dev/pa"}  # bare string → {repo}
    assert portfolio._archetypes({}) == {}


@pytest.mark.asyncio
async def test_spinup_archetype_fills_repo_and_gate(fleet, template, tmp_path):
    """spinup_team(archetype=...) pulls repo + gate from the preset (no repo arg needed)."""
    repo = tmp_path / "pcrepo"
    repo.mkdir()
    cfg = {
        "team_template": str(template),
        "team_archetypes": {"protocontent": {"repo": str(repo), "gate": "pnpm -r build"}},
    }
    out = json.loads(await _tool("portfolio_spinup_team", cfg).ainvoke({"name": "pc1", "archetype": "protocontent"}))
    assert out["team"] == "pc1" and out["repo"] == str(repo) and out["gate"] == "pnpm -r build"
    bound = (tmp_path / "ws" / "pc1" / "langgraph-config.yaml").read_text()
    assert str(repo) in bound and "pnpm -r build" in bound


@pytest.mark.asyncio
async def test_spinup_unknown_archetype(fleet, template):
    out = await _tool("portfolio_spinup_team", {"team_archetypes": {"pc": {"repo": "/r"}}}).ainvoke(
        {"name": "x", "archetype": "nope"}
    )
    assert "no team archetype 'nope'" in out and "pc" in out  # lists the known ones


def test_portfolio_archetypes_tool_lists_them():
    cfg = {"team_archetypes": {"pc": {"repo": "/dev/pc", "gate": "build"}}}
    out = json.loads(_tool("portfolio_archetypes", cfg).invoke({}))
    assert out == {"pc": {"repo": "/dev/pc", "gate": "build"}}


# ── board isolation — a spawned team gets its OWN beads DB, not the repo's .beads ──


@pytest.mark.asyncio
async def test_spinup_isolates_the_board_by_default(fleet, template, tmp_path, monkeypatch):
    """Default: project_board.db_path → the team's own scoped beads DB (NOT the repo's
    .beads), and the repo `br init` is skipped — so the team never sees/resumes/pollutes the
    repo's native board."""
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    inits: list = []
    monkeypatch.setattr(portfolio, "_beads_init", lambda r: inits.append(r))
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke({"name": "alpha", "repo": str(repo), "template": str(template)})
    )
    assert out["board"] == "isolated"
    doc = yaml.safe_load((tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text())
    assert doc["project_board"]["db_path"].endswith("board.beads.db")  # scoped, not the repo's .beads
    assert inits == []  # repo `br init` skipped (the board isn't the repo's .beads)


@pytest.mark.asyncio
async def test_spinup_shared_board_uses_the_repo_beads(fleet, template, tmp_path, monkeypatch):
    """Opt-in: shared_board=True → the repo's own .beads (br init runs, no db_path) — for a
    team that IS the repo's dev team."""
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    inits: list = []
    monkeypatch.setattr(portfolio, "_beads_init", lambda r: inits.append(r))
    out = json.loads(
        await _tool("portfolio_spinup_team").ainvoke(
            {"name": "alpha", "repo": str(repo), "template": str(template), "shared_board": True}
        )
    )
    assert out["board"] == "shared (repo .beads)"
    doc = yaml.safe_load((tmp_path / "ws" / "alpha" / "langgraph-config.yaml").read_text())
    assert "db_path" not in (doc.get("project_board") or {})  # uses the repo's .beads
    assert inits == [out["repo"]]  # repo `br init` ran


def test_set_board_db_sets_and_respects_template(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text("project_board:\n  repo: /r\n")
    portfolio._set_board_db(p, "/scoped/board.db")
    assert yaml.safe_load(p.read_text())["project_board"]["db_path"] == "/scoped/board.db"
    # a template that pins its own db_path is respected
    p.write_text("project_board:\n  db_path: /mine.db\n")
    portfolio._set_board_db(p, "/scoped/board.db")
    assert yaml.safe_load(p.read_text())["project_board"]["db_path"] == "/mine.db"
