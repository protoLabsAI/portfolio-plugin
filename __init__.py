"""portfolio — the PM / program orchestration layer (ADR 0055 P1).

One agent orchestrates work across MANY team-agents, each running its own project
board for its own repo (scale-out). This is **pure composition** of three existing
subsystems — no new dispatch or registry machinery:

  - fleet (`graph.fleet.supervisor`) — the team-agent registry (remote members)
  - delegates (`plugins.delegates`) — the A2A dispatch primitive (`A2aAdapter`)
  - project_board (its data router) — the structured remote board read

The PM treats each remote fleet member as a *board* addressed by its name: list
them (`portfolio_boards`), dispatch a feature to one over A2A (`portfolio_dispatch`),
read one back structured (`portfolio_board_read`), see a bounded cross-board rollup
(`portfolio_rollup`), watch for changes without polling (`portfolio_watch` +
`portfolio_diff`), and sequence cross-board dependencies (`portfolio_link` +
`portfolio_plan` + `portfolio_autodispatch` — hold work behind a cross-board blocker,
then create it once the blocker ships). See ADR 0055.

P2 deltas are PULL-DIFF, not push: the PM snapshots each board (state per feature)
and reports what changed since the last check. A2A push notifications are task-scoped
(wrong granularity), the event bus is in-process, and a team-agent doesn't know its
PM — so a PM-side snapshot+diff, run on a schedule, is the thin correct shape (ADR
0055 P2; the optional inbox-push upgrade is deferred).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.tools import tool


def register(registry) -> None:
    cfg = getattr(registry, "config", None) or {}
    for t in _tools(cfg):
        registry.register_tool(t)
    # Console view (ADR 0026) — a Portfolio dashboard. Two routers at DISTINCT prefixes:
    # the PUBLIC page (iframe src carries no bearer) + the GATED data route. Best-effort:
    # a view failure must never sink the tools.
    try:
        from .view import build_data_router, build_view_router

        registry.register_router(build_view_router(), prefix="/plugins/portfolio")
        registry.register_router(build_data_router(cfg), prefix="/api/plugins/portfolio")
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("[portfolio] view registration failed")


def _remote_by_name(name: str) -> dict | None:
    """The board record ``{name, id, url, token}`` for a board name/id, or None.

    A board is either a REMOTE fleet member (an external instance — carries a stored
    bearer) or a LOCAL fleet member (a workspace this PM spawned + runs — addressed on
    ``127.0.0.1:<port>``, no token). Remotes are checked first (they carry auth) via
    ``list_remotes()``; local members come from ``status()``, which a spawned team joins
    automatically once started — so a locally-spawned team is dispatchable WITHOUT being
    (mis)registered as a remote, which would collide with its own workspace name.
    """
    from graph.fleet import supervisor

    name = (name or "").strip()
    if not name:
        return None
    for rec in supervisor.list_remotes():
        if rec.get("name") == name or rec.get("id") == name:
            return rec
    for m in supervisor.status():
        if m.get("host") or m.get("remote"):
            continue  # the PM itself / remotes already handled above
        if (m.get("name") == name or m.get("id") == name) and m.get("port"):
            return {"name": m["name"], "id": m.get("id"), "url": f"http://127.0.0.1:{m['port']}", "token": ""}
    return None


def _all_board_recs() -> list[dict]:
    """Every board the PM can reach — REMOTE members (carry a bearer) AND LOCAL spawned-team
    members (``127.0.0.1:<port>``). The rollup / diff / plan paths were written for the
    remote-only model (``list_remotes``); a spawned team is a LOCAL member (``status``), so
    those paths silently missed it — boards showed empty, cross-board links read as
    ``dangling``. Resolving every name through ``_remote_by_name`` unifies the two (one
    deduped rec per board, the remote's token preserved)."""
    from graph.fleet import supervisor

    names: list[str] = [r.get("name") for r in supervisor.list_remotes()]
    try:
        for m in supervisor.status():
            if m.get("host") or m.get("remote"):
                continue  # the PM itself / remotes already counted above
            if m.get("name"):
                names.append(m["name"])
    except Exception:  # noqa: BLE001 — a status hiccup shouldn't blank the boards
        pass
    seen: set = set()
    recs: list[dict] = []
    for n in names:
        if not n or n in seen:
            continue
        seen.add(n)
        rec = _remote_by_name(n)
        if rec:
            recs.append(rec)
    return recs


class _BoardUnavailable(Exception):
    """A team board couldn't be read (policy block, 404, HTTP error, network)."""


async def _fetch_board_features(rec: dict, state: str = "") -> list:
    """GET a remote team board's features (structured) — the shared read used by both
    the raw read and the rollup. Raises ``_BoardUnavailable`` so callers format their
    own message. The stored remote bearer authenticates both ``/a2a`` and the board
    API; the remote was egress-vetted at add_remote, re-checked here for parity with
    the A2A dispatch path."""
    url = rec["url"].rstrip("/") + "/api/plugins/project_board/features"
    from security import policy

    blocked = policy.check_url(url)
    if blocked:
        raise _BoardUnavailable(blocked)

    import httpx

    headers = {"Authorization": f"Bearer {rec['token']}"} if rec.get("token") else {}
    params = {"state": state} if state else None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers, params=params)
    except Exception as exc:  # noqa: BLE001
        raise _BoardUnavailable(str(exc)) from exc
    if r.status_code == 404:
        raise _BoardUnavailable("no project board exposed (project_board not enabled there)")
    if r.status_code >= 400:
        raise _BoardUnavailable(f"HTTP {r.status_code} {r.text[:200]}")
    return r.json().get("features", [])


def _rollup_one(name: str, features: list) -> dict:
    """Project a board's features into a BOUNDED rollup — lane counts + only the
    blocked / foundation (critical-path) items, never the full feature list. This is
    what keeps a PM's context small when reasoning over many boards."""
    counts: dict[str, int] = {}
    blocked: list[dict] = []
    critical: list[dict] = []
    for f in features:
        st = f.get("board_state", "backlog")
        counts[st] = counts.get(st, 0) + 1
        if f.get("blocked") or f.get("dag_blocked"):
            blocked.append({"id": f.get("id"), "title": f.get("title", "")})
        if f.get("foundation") and st != "done":
            critical.append({"id": f.get("id"), "title": f.get("title", ""), "state": st})
    return {"board": name, "total": len(features), "counts": counts, "blocked": blocked, "critical_path": critical}


def _parse_boards(boards: str) -> set | None:
    """Comma-separated board filter → a set of names, or None for all."""
    return {b.strip() for b in boards.split(",") if b.strip()} if boards else None


# ── P2 deltas: snapshot + diff (pull-diff, PM-side) ──────────────────────────────


def _snapshot_path():
    """Per-instance baseline for delta detection — scoped under the PM's data root so
    co-located instances don't collide (ADR 0004), mirroring remotes.json."""
    from infra.paths import data_home, scope_leaf

    return scope_leaf(data_home() / "portfolio_snapshot.json")


def _load_snapshot() -> dict:
    p = _snapshot_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001 — a corrupt snapshot just re-baselines, never breaks the tool
        return {}


def _save_snapshot(snap: dict) -> None:
    p = _snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap))


def _index_features(features: list) -> dict:
    """feature_id → the fields a diff cares about (state + blocked + title)."""
    return {
        f["id"]: {
            "state": f.get("board_state"),
            "blocked": bool(f.get("blocked") or f.get("dag_blocked")),
            "title": f.get("title", ""),
        }
        for f in features
        if f.get("id")
    }


def _diff_boards(prev: dict, curr: dict) -> dict:
    """Compare two feature-index snapshots → only the meaningful transitions: a feature
    reaching ``done`` (PR merged), newly blocked, unblocked, or newly appearing."""
    merged, newly_blocked, unblocked, new = [], [], [], []
    for fid, c in curr.items():
        p = prev.get(fid)
        if p is None:
            new.append({"id": fid, "title": c["title"], "state": c["state"]})
            continue
        if c["state"] == "done" and p.get("state") != "done":
            merged.append({"id": fid, "title": c["title"]})
        if c["blocked"] and not p.get("blocked"):
            newly_blocked.append({"id": fid, "title": c["title"]})
        elif p.get("blocked") and not c["blocked"]:
            unblocked.append({"id": fid, "title": c["title"]})
    out = {}
    if merged:
        out["merged"] = merged
    if newly_blocked:
        out["newly_blocked"] = newly_blocked
    if unblocked:
        out["unblocked"] = unblocked
    if new:
        out["new"] = new
    return out


async def _compute_portfolio_diff(wanted: set | None) -> dict:
    """Fan out across the (filtered) team boards, diff each against the saved baseline,
    and rewrite the baseline. Returns ``{recs, first_run, changes}``. On the first run
    (no baseline) it records the baseline and reports nothing — there's no 'before'."""
    recs = [r for r in _all_board_recs() if wanted is None or r.get("name") in wanted or r.get("id") in wanted]
    snap = _load_snapshot()
    first_run = not snap

    async def _one(rec: dict):
        name = rec.get("name")
        try:
            feats = await _fetch_board_features(rec)
        except _BoardUnavailable as exc:
            return name, {"error": str(exc)}, None
        idx = _index_features(feats)
        return name, _diff_boards(snap.get(name, {}), idx), idx

    results = await asyncio.gather(*[_one(r) for r in recs]) if recs else []
    changes = {}
    for name, deltas, idx in results:
        if idx is not None:  # only advance the baseline for boards we actually read
            snap[name] = idx
        if deltas and not first_run:  # first run = pure baseline, suppress the all-new noise
            changes[name] = deltas
    _save_snapshot(snap)
    return {"recs": len(recs), "first_run": first_run, "changes": changes}


# ── P3 cross-board dependency graph (PM-side links + sequencing) ──────────────────


async def _fetch_all(recs: list) -> tuple[dict, dict]:
    """Fetch every board's features concurrently → (features_by_board, unreachable{name:error})."""

    async def _one(rec: dict):
        name = rec.get("name")
        try:
            return name, await _fetch_board_features(rec), None
        except _BoardUnavailable as exc:
            return name, None, str(exc)

    results = await asyncio.gather(*[_one(r) for r in recs]) if recs else []
    by_board, unreachable = {}, {}
    for name, feats, err in results:
        if err is None:
            by_board[name] = feats
        else:
            unreachable[name] = err
    return by_board, unreachable


def _file_lock(path):
    """A cross-process lock around a registry file's READ-MODIFY-WRITE (links / teams).
    Several tool calls can run concurrently in one agent turn (the LLM fires them in
    parallel), and an unlocked load→append→save loses an entry — that's how two
    portfolio_link calls in one turn dropped one. ``filelock`` is host-provided; if it's
    absent (an odd env) degrade to a no-op so single-call paths still work."""
    try:
        from filelock import FileLock

        return FileLock(str(path) + ".lock", timeout=10)
    except Exception:  # noqa: BLE001 — no filelock ⇒ best-effort (uncontended single calls are fine)
        import contextlib

        return contextlib.nullcontext()


def _links_path():
    """Cross-board dependency edges, scoped under the PM's data root (ADR 0004) —
    mirrors the P2 snapshot + fleet remotes.json. Edges are PM state: a team-agent
    doesn't know its dependents, and the sequencing is the PM's concern."""
    from infra.paths import data_home, scope_leaf

    return scope_leaf(data_home() / "portfolio_links.json")


def _load_links() -> list:
    p = _links_path()
    try:
        return json.loads(p.read_text()) if p.exists() else []
    except Exception:  # noqa: BLE001 — a corrupt links file shouldn't break the tools
        return []


def _save_links(links: list) -> None:
    from infra.paths import atomic_write

    p = _links_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(links, indent=2))


def _edge_id(from_board: str, from_feature: str, to_board: str, to_feature: str) -> str:
    """Stable id from the edge tuple → natural dedup (the same edge always gets the same id)."""
    import hashlib

    key = f"{from_board}:{from_feature}>{to_board}:{to_feature}"
    return "lnk-" + hashlib.sha1(key.encode()).hexdigest()[:8]


def _has_cycle(links: list) -> bool:
    """DFS over (board, feature) nodes — True if the edge set contains a cycle (a feature
    transitively depends on itself). Used to REJECT a cycle at link time; without this a
    cycle would silently deadlock (nothing ever becomes ready)."""
    from collections import defaultdict

    adj: dict = defaultdict(list)
    for ln in links:
        adj[(ln["from_board"], ln["from_feature"])].append((ln["to_board"], ln["to_feature"]))
    color: dict = {}  # absent/0 = unvisited, 1 = on the current path, 2 = done

    def visit(node) -> bool:
        color[node] = 1
        for nxt in adj.get(node, []):
            c = color.get(nxt, 0)
            if c == 1 or (c == 0 and visit(nxt)):
                return True
        color[node] = 2
        return False

    return any(color.get(n, 0) == 0 and visit(n) for n in list(adj))


async def _compute_plan() -> dict:
    """The cross-board dependency graph + what's ready to dispatch next. Shared by the
    portfolio_plan tool AND the dashboard's dependency view (one source of truth). Returns
    ``{links, ready_to_dispatch, blocked}`` — ``links`` is ``[]`` when none are recorded.
    Each link carries a ``status``: ``satisfied`` (the depended-on feature is done),
    ``blocking`` (not yet), ``unknown`` (its board is unreachable — never assumed
    satisfied), or ``dangling`` (the board/feature no longer exists)."""
    links = _load_links()
    if not links:
        return {"links": [], "ready_to_dispatch": [], "blocked": []}
    by_board, unreachable = await _fetch_all(_all_board_recs())
    state = {}
    for name, feats in by_board.items():
        for f in feats:
            if f.get("id"):
                state[(name, f["id"])] = f.get("board_state")

    def status_of(ln) -> str:
        if ln["to_board"] in unreachable:
            return "unknown"  # fail-closed: an unreadable blocker is NOT satisfied
        key = (ln["to_board"], ln["to_feature"])
        if key not in state:
            return "dangling"
        return "satisfied" if state[key] == "done" else "blocking"

    enriched = []
    for ln in links:
        e = {
            "id": ln["id"],
            "from_board": ln["from_board"],
            "from_feature": ln["from_feature"],
            "to_board": ln["to_board"],
            "to_feature": ln["to_feature"],
            "status": status_of(ln),
            "to_state": state.get((ln["to_board"], ln["to_feature"])),
        }
        if ln.get("spec"):  # a planned-dispatch link (portfolio_autodispatch creates it when satisfied)
            e["planned"] = True
            e["dispatched"] = bool(ln.get("dispatched"))
        enriched.append(e)

    from collections import defaultdict

    by_from: dict = defaultdict(list)
    for e in enriched:
        if e.get("dispatched"):
            continue  # already auto-dispatched — the held work was created on its board
        by_from[(e["from_board"], e["from_feature"])].append(e)
    ready, blocked = [], []
    for (fb, ff), edges in by_from.items():
        if all(e["status"] == "satisfied" for e in edges):
            from_state = state.get((fb, ff))
            if from_state in (None, "backlog", "ready"):  # not yet underway → dispatchable
                ready.append({"board": fb, "feature": ff, "state": from_state})
        else:
            blocked.append(
                {
                    "board": fb,
                    "feature": ff,
                    "blockers": [
                        {
                            "board": e["to_board"],
                            "feature": e["to_feature"],
                            "status": e["status"],
                            "to_state": e["to_state"],
                        }
                        for e in edges
                        if e["status"] != "satisfied"
                    ],
                }
            )
    return {"links": enriched, "ready_to_dispatch": ready, "blocked": blocked}


def _dispatch_instruction(title: str, spec: str, acceptance_criteria: str, files_to_modify: str) -> str:
    lines = [
        "You manage a project board (the project_board plugin). Create a new feature on it "
        "and mark it ready so your spawn loop picks it up — use board_create_feature then "
        "board_mark_ready.",
        f"Title: {title}",
        f"Spec: {spec}",
    ]
    if acceptance_criteria:
        lines.append(f"Acceptance criteria: {acceptance_criteria}")
    if files_to_modify:
        lines.append(f"Files to modify: {files_to_modify}")
    lines.append("Report the created feature id and its board state.")
    return "\n".join(lines)


async def _a2a_create_feature(
    rec: dict, title: str, spec: str, acceptance_criteria: str = "", files_to_modify: str = ""
) -> str:
    """Dispatch a 'create + ready a feature' instruction to a team board over A2A and
    return the team lead's reply. Shared by portfolio_dispatch and portfolio_autodispatch.
    Raises on a dispatch error (the caller formats the message)."""
    from plugins.delegates.adapters import ADAPTERS, Delegate

    d = Delegate(
        name=rec["name"],
        type="a2a",
        url=rec["url"].rstrip("/") + "/a2a",
        auth_scheme="bearer",
        auth_token=rec.get("token", ""),
    )
    return await ADAPTERS["a2a"].dispatch(
        d, _dispatch_instruction(title, spec, acceptance_criteria, files_to_modify), timeout=120
    )


# ── Ephemeral engineering teams (spin up / tear down / auto-dispose) ──────────────
# The PM spawns a finite-lifetime team for a project: clone a base TEAM config template
# into a scoped workspace (ADR 0041), bind the repo, start the agent (ADR 0042 fleet
# supervisor), and register it as a remote board — so the existing portfolio_* tools
# dispatch to it. Tear it down by hand (portfolio_teardown_team) or let it self-dispose
# when its board drains (portfolio_autodispose). All in-process over the SAME primitives
# team-up.sh uses by hand: graph.workspaces.manager + graph.fleet.supervisor.


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _teams_path():
    """Registry of teams THIS PM spawned — scoped under the PM's data root (ADR 0004),
    mirroring portfolio_links.json / portfolio_snapshot.json. It's how teardown +
    autodispose tell a portfolio-spawned ephemeral team from a hand-registered remote
    (which must never be auto-disposed)."""
    from infra.paths import data_home, scope_leaf

    return scope_leaf(data_home() / "portfolio_teams.json")


def _load_teams() -> list:
    p = _teams_path()
    try:
        return json.loads(p.read_text()) if p.exists() else []
    except Exception:  # noqa: BLE001 — a corrupt registry shouldn't break the tools
        return []


def _save_teams(teams: list) -> None:
    from infra.paths import atomic_write

    p = _teams_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(teams, indent=2))


def _record_team(rec: dict) -> None:
    with _file_lock(_teams_path()):  # serialize concurrent spinups (RMW on the registry)
        teams = [t for t in _load_teams() if t.get("name") != rec["name"]]
        teams.append(rec)
        _save_teams(teams)


def _forget_team(name: str) -> None:
    with _file_lock(_teams_path()):  # serialize against a concurrent spinup/teardown
        _save_teams([t for t in _load_teams() if t.get("name") != name])


def _team_by_name(name: str) -> dict | None:
    return next((t for t in _load_teams() if t.get("name") == name), None)


def _apply_team_bindings(cfg_path: Path, repo: str, name: str, gate: str) -> None:
    """Fill the per-spawn sentinels in the cloned team config (comment-preserving, the
    same plain-string-replace team-up.sh uses): ``{{REPO}}`` → the repo path (only when a
    repo is given, so a prebuilt repo-baked template is left untouched), ``{{TEAM_NAME}}``
    → the team name, ``{{GATE}}`` → the pre-PR gate command (or empty)."""
    t = cfg_path.read_text()
    if repo:
        t = t.replace("{{REPO}}", repo)
    t = t.replace("{{TEAM_NAME}}", name)
    t = t.replace("{{GATE}}", gate or "")
    cfg_path.write_text(t)


def _beads_init(repo: str) -> None:
    """Best-effort ``br init`` so the team's board pins to its repo (not a parent dir's
    .beads). Guarded + non-fatal: skipped if the repo already has a .beads or ``br``
    isn't installed — the board still runs, it just may resolve a parent workspace."""
    import os
    import subprocess

    if (Path(repo) / ".beads").exists():
        return
    br = os.environ.get("BR_BIN", "br")
    try:
        subprocess.run([br, "init"], cwd=repo, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _detect_gate(repo: str) -> str:
    """The repo's real pre-PR check command, inferred from its build files — so the team's
    coder must pass the project's own gate before a PR opens, even when the caller didn't
    pass one. Same heuristics as team-up.sh: a Python project lints, a VitePress site
    builds the docs, a generic Node project tests. Empty when nothing's recognized (the
    onboard-project skill can still set a smarter one)."""
    r = Path(repo)
    if (r / "pyproject.toml").exists() or (r / "requirements-dev.txt").exists():
        return "ruff check . && ruff format --check ."
    pkg = r / "package.json"
    if pkg.exists():
        try:
            text = pkg.read_text()
        except OSError:
            text = ""
        if '"vitepress"' in text:
            return "npm ci && npm run docs:build"
        return "npm ci && npm test"
    return ""


def _onboard_instruction(repo: str) -> str:
    """The one-time 'get this repo ready' brief sent to a freshly-spawned team. Reaches the
    team as an A2A message (the channel a spawned team reliably reads — its persona falls
    back to the host's SOUL, so readiness rides on this instruction + the team's loaded
    onboard-project skill, not on a per-team persona)."""
    return (
        "Before any feature work, get this repo ready for the board. Run your onboard-project "
        f"skill on the repo at {repo}:\n"
        "- Scan it (stack, build/test command, .gitignore, grounding doc, git posture).\n"
        "- Auto-fix the safe, deterministic gaps directly via your coder (e.g. a grounding/"
        "PROTO.md doc if missing, ignoring build output) — do NOT blanket-ignore .proto (its "
        "evolve/ holds versioned skills; only session scratch is ignored, already handled).\n"
        "- Board the judgment gaps (a grounding doc, PR CI) as features if they need real work.\n"
        "Report a short readiness summary: PASS / FIXED / BOARDED per item. Do not invent work "
        "beyond readiness — the actual task will be dispatched separately."
    )


async def _a2a_send(base: str, instruction: str, *, token: str = "", timeout: int = 240) -> str:
    """Send a plain instruction to a team over A2A and return its reply. Used for the
    one-time onboarding kick (portfolio_dispatch uses the create-feature wrapper instead)."""
    from plugins.delegates.adapters import ADAPTERS, Delegate

    d = Delegate(
        name="team",
        type="a2a",
        url=base.rstrip("/") + "/a2a",
        auth_scheme="bearer" if token else "",
        auth_token=token,
    )
    return await ADAPTERS["a2a"].dispatch(d, instruction, timeout=timeout)


# The proto coding agent writes a repo-level .proto/ dir mixing PER-SESSION SCRATCH
# (memory/, session-notes.md, repo-map-cache.json) with protoCLI-MANAGED state
# (.proto/evolve/ holds skills). A blanket `.proto/` ignore would hide the skills, so we
# ignore ONLY the session artifacts — otherwise the coder leaks them into its PR (bug #49).
_PROTO_SCRATCH_MARKER = "# proto coding-agent per-session scratch"
_PROTO_SCRATCH_BLOCK = (
    f"\n{_PROTO_SCRATCH_MARKER} (NOT the whole .proto — .proto/evolve holds protoCLI-managed\n"
    "# skills that should be versioned; only the session artifacts are ignored)\n"
    ".proto/memory/\n"
    ".proto/session-notes.md\n"
    ".proto/repo-map-cache.json\n"
)


def _exclude_proto_scratch(repo: str) -> None:
    """Make the team's repo + the coder's worktrees ignore the proto coding agent's
    per-session scratch so its PRs don't leak it (bug #49) — WITHOUT ignoring the whole
    ``.proto/`` dir, which also holds protoCLI-managed skills (``.proto/evolve``) that should
    be versioned.

    Writes to ``.git/info/exclude`` (NOT ``.gitignore``): it's per-repo, never committed (no
    churn in the repo's tracked files), and — crucially — shared by every worktree via the
    common git dir, so the coder's branch worktree honors it. An uncommitted ``.gitignore``
    in the main tree would NOT reach that worktree (a fresh checkout off HEAD). Idempotent
    (marker-guarded); main repo only (a worktree's ``.git`` is a file). Best-effort."""
    try:
        git = Path(repo) / ".git"
        if not git.is_dir():
            return
        info = git / "info"
        info.mkdir(parents=True, exist_ok=True)
        excl = info / "exclude"
        text = excl.read_text() if excl.exists() else ""
        if _PROTO_SCRATCH_MARKER in text:
            return
        if text and not text.endswith("\n"):
            text += "\n"
        excl.write_text(text + _PROTO_SCRATCH_BLOCK)
    except OSError:
        pass


def _host_plugins_dir() -> str:
    """The PM host's live plugins dir (where its git-installed plugins live). The default
    ``plugins.dir`` for a spawned team, so it discovers the SAME external plugins the host
    already has — project_board, github — without a per-workspace reinstall. (delegates is
    builtin and plugin-devkit is in-tree under REPO_ROOT/plugins, so both load free in any
    workspace; only external plugins need a discovery root.)"""
    from graph.plugins.installer import live_plugins_dir

    return str(live_plugins_dir())


def _default_template() -> str:
    """The plugin's own shipped example team template — the fallback when neither a
    ``template=`` arg nor ``portfolio.team_template`` is set, so portfolio_spinup_team works
    out of the box. It's a generic project_board + delegates team; point team_template at a
    creds-filled copy (gateway key + model.api_base) for teams that run real model turns."""
    p = Path(__file__).parent / "examples" / "team-template"
    return str(p) if (p / "langgraph-config.yaml").exists() else ""


def _ensure_plugins_dir(cfg_path: Path, plugins_dir: str) -> None:
    """Point the cloned config's ``plugins.dir`` at a directory holding the external
    plugins the team enables (project_board / github), so they actually load — a workspace
    created from a config (not a bundle) has no plugins/ of its own. Respect a ``dir`` the
    template already declares; otherwise set it. Comment-preserving (ruamel)."""
    if not plugins_dir:
        return
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc(cfg_path)
    if not isinstance(doc, dict):
        return
    plugins = doc.setdefault("plugins", {})
    if not isinstance(plugins, dict) or plugins.get("dir"):
        return  # operator's template already chose a plugins dir — don't override
    plugins["dir"] = plugins_dir
    save_yaml_doc(doc, cfg_path)


def _set_board_db(cfg_path: Path, db: str) -> None:
    """Isolate the team's board: point ``project_board.db_path`` at the team's OWN beads DB
    (in its scoped workspace) instead of the repo's committed ``.beads``. So a spawned team
    never sees / resumes the repo's native board and never pollutes it — the board is
    ephemeral PM tracking, purged with the workspace on teardown. projectBoard-plugin honors
    db_path in both the loop and the tools, and skips the repo ``br init`` when it's set.
    Respect a db_path the template already declares. Comment-preserving (ruamel)."""
    if not db:
        return
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc(cfg_path)
    if not isinstance(doc, dict):
        return
    pb = doc.setdefault("project_board", {})
    if not isinstance(pb, dict) or pb.get("db_path"):
        return  # template pinned its own board db — don't override
    pb["db_path"] = db
    save_yaml_doc(doc, cfg_path)


async def _await_ready(base: str, timeout: float = 40.0) -> bool:
    """Poll a freshly-started team's ``/healthz`` until it's up (or timeout). A spawned
    server boots its plugins asynchronously, so the returned A2A endpoint isn't dispatch-
    able the instant start() returns — this makes the tool hand back a usable endpoint."""
    import time

    import httpx

    end = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=3) as client:
        while time.monotonic() < end:
            try:
                r = await client.get(f"{base}/healthz")
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 — not up yet; keep polling
                pass
            await asyncio.sleep(1.0)
    return False


def _dispose(team: dict) -> dict:
    """Stop the team's server, purge its workspace + scoped data, unregister the fleet
    remote, and forget it. Best-effort per step (a partial failure still cleans what it
    can). The managed repo + any PRs it opened are UNTOUCHED. Blocking (supervisor.stop
    busy-waits) — call via ``asyncio.to_thread``."""
    from graph.fleet import supervisor
    from graph.workspaces import manager

    out = {"team": team["name"]}
    try:
        supervisor.stop(team["id"])
        out["stopped"] = True
    except Exception as exc:  # noqa: BLE001 — already-stopped/unknown is fine; report it
        out["stop_error"] = str(exc)
    try:
        manager.remove(team["id"], purge=True)
        out["purged"] = True
    except Exception as exc:  # noqa: BLE001
        out["remove_error"] = str(exc)
    try:
        supervisor.remove_remote(team["name"])
        out["unregistered"] = True
    except Exception:  # noqa: BLE001 — remote may already be gone
        pass
    _forget_team(team["name"])
    return out


def _freshen_repo(repo: str) -> str:
    """Bring the repo up to date with its remote BEFORE a team works on it — ``git fetch``
    + fast-forward the checked-out default branch when it's safe (clean tree, on that
    branch). So the team's coder branches its per-feature worktrees off CURRENT code, not a
    stale HEAD (which is how a team ends up reworking already-merged changes / hitting
    conflicts). Best-effort + non-fatal: no remote / offline / dirty / diverged just skips
    the fast-forward (the fetch still refreshes ``origin/*`` for worktrees that branch off
    it). Returns a short status for the spawn result."""
    import subprocess

    def git(*args, timeout=120):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)

    try:
        if git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return "not a git repo"
        if not git("remote").stdout.strip():
            return "no remote — left as is"
        f = git("fetch", "--prune", "origin")
        if f.returncode != 0:
            return f"fetch failed: {(f.stderr or '').strip()[:120]}"
        head = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        default = head.split("/", 1)[1] if head.startswith("origin/") else "main"
        cur = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if cur != default:
            return f"fetched; on '{cur}' not '{default}' — left as is"
        if git("status", "--porcelain").stdout.strip():
            return f"fetched; '{default}' has local changes — not fast-forwarded"
        ff = git("merge", "--ff-only", f"origin/{default}")
        if ff.returncode != 0:
            return f"fetched; '{default}' diverged from origin — not fast-forwarded"
        return f"up to date ('{default}' fast-forwarded to origin)"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"freshen skipped: {exc}"


def _archetypes(cfg: dict) -> dict:
    """Named team presets — prebuilt teams for the repos a PM works OFTEN, so
    ``spinup_team(archetype="protocontent")`` needs no repo/gate/template each time. From
    ``portfolio.team_archetypes`` — ``{name: {repo, gate?, template?, plugins_dir?}}`` (a
    bare string value is shorthand for ``{repo: <that>}``)."""
    raw = (cfg or {}).get("team_archetypes") or {}
    out: dict = {}
    if isinstance(raw, dict):
        for name, spec in raw.items():
            if isinstance(spec, dict):
                out[name] = spec
            elif isinstance(spec, str):
                out[name] = {"repo": spec}
    return out


async def _spinup_team(
    name: str,
    repo: str = "",
    template: str = "",
    gate: str = "",
    port: int = 0,
    auto_dispose: bool = True,
    plugins_dir: str = "",
    onboard: bool = True,
    archetype: str = "",
    shared_board: bool = False,
    *,
    cfg: dict | None = None,
) -> dict:
    """Core of the spin-up flow — shared by the portfolio_spinup_team tool AND the
    dashboard's spin-up button (one source of truth). Returns the result dict, or
    ``{"error": "<message>"}`` on a validation / spawn failure (the tool surfaces the
    string; the route returns the dict)."""
    cfg = cfg or {}
    from graph.fleet import supervisor
    from graph.workspaces import manager

    name = (name or "").strip()
    if not name:
        return {"error": "Error: a team name is required."}
    if _remote_by_name(name) is not None or _team_by_name(name) is not None:
        return {"error": f"Error: a team/board named {name!r} already exists. Pick another name or tear it down first."}
    # Archetype = a named (repo, gate, template) preset for a frequently-worked repo; fill
    # the unset args from it (explicit args still win).
    if archetype:
        arch = _archetypes(cfg).get(archetype)
        if arch is None:
            known = ", ".join(_archetypes(cfg)) or "(none configured)"
            return {"error": f"Error: no team archetype {archetype!r}. Known archetypes: {known}."}
        repo = repo or arch.get("repo", "")
        gate = gate or arch.get("gate", "")
        template = template or arch.get("template", "")
        plugins_dir = plugins_dir or arch.get("plugins_dir", "")
    tmpl = (template or "").strip() or str(cfg.get("team_template", "") or "") or _default_template()
    if not tmpl:
        return {
            "error": (
                "Error: no team template. Pass template=<path to a base team langgraph-config.yaml> or set "
                "portfolio.team_template in config. The template carries project_board + delegates + the coder "
                "ladder; its {{REPO}} / {{TEAM_NAME}} / {{GATE}} sentinels are filled per spawn."
            )
        }
    if not Path(tmpl).expanduser().exists():
        return {"error": f"Error: team template not found: {tmpl}"}
    repo_abs = ""
    if repo:
        repo_abs = str(Path(repo).expanduser().resolve())
        if not Path(repo_abs).is_dir():
            return {"error": f"Error: repo path not found: {repo}"}
    # Gate: caller's value, else auto-detect from the repo so the coder must pass the
    # project's own check before a PR — even when the caller didn't specify one.
    gate = (gate or "").strip() or (_detect_gate(repo_abs) if repo_abs else "")
    pdir = (plugins_dir or "").strip() or str(cfg.get("team_plugins_dir", "") or "") or _host_plugins_dir()

    # 1. clone the template into a scoped workspace (config + secrets, identity restamped)
    try:
        ws = await asyncio.to_thread(manager.create, name, from_config=tmpl, port=(port or None))
    except Exception as exc:  # noqa: BLE001 — surface the create failure
        return {"error": f"Error creating team workspace: {exc}"}
    wid = ws["id"]
    assigned = ws["port"]

    # 2. bind the repo + point at the external plugins + ISOLATE the board (its own beads DB,
    # not the repo's .beads — so the team never sees/resumes the repo's native board nor
    # pollutes it) + ignore the coder's .proto scratch + FRESHEN the repo so the team works on
    # current code; roll back on failure
    freshness = "n/a (no repo)"
    board_mode = "shared (repo .beads)" if shared_board else "isolated"
    try:
        cfg_path = Path(ws["path"]) / "langgraph-config.yaml"
        _apply_team_bindings(cfg_path, repo_abs, name, gate)
        _ensure_plugins_dir(cfg_path, pdir)
        if not shared_board:
            # Isolated (default): the team's board lives in its own scoped workspace, purged
            # on teardown. projectBoard-plugin honors db_path + skips the repo `br init`.
            # beads requires the db path to sit under a `.beads/` dir — create it.
            board_db = Path(ws["path"]) / ".beads" / "board.db"
            board_db.parent.mkdir(parents=True, exist_ok=True)
            _set_board_db(cfg_path, str(board_db))
        if repo_abs:
            if shared_board:
                _beads_init(repo_abs)  # opt-in: the team IS this repo's dev team → repo board
            _exclude_proto_scratch(repo_abs)
            freshness = await asyncio.to_thread(_freshen_repo, repo_abs)
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(manager.remove, wid, purge=True)
        return {"error": f"Error binding repo into team config: {exc}"}

    # 3. start the agent (it joins the fleet as a local member) + wait for it to come up.
    try:
        rec = await asyncio.to_thread(supervisor.start, wid)
        assigned = rec.get("port", assigned)
        base = f"http://127.0.0.1:{assigned}"
        ready = await _await_ready(base)
    except Exception as exc:  # noqa: BLE001 — best-effort rollback so a retry isn't poisoned
        try:
            await asyncio.to_thread(supervisor.stop, wid)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.to_thread(manager.remove, wid, purge=True)
        return {"error": f"Error starting team: {exc}"}

    _record_team(
        {
            "name": name,
            "id": wid,
            "port": assigned,
            "repo": repo_abs,
            "auto_dispose": bool(auto_dispose),
            "spawned_at": _now(),
        }
    )

    # 4. one-time onboarding (best-effort; never fails the spawn).
    onboarding = "skipped"
    if onboard and repo_abs and ready:
        try:
            reply = await _a2a_send(base, _onboard_instruction(repo_abs))
            onboarding = reply.strip()[:600] or "done (no summary returned)"
        except Exception as exc:  # noqa: BLE001 — onboarding is best-effort
            onboarding = f"attempted, but errored (team is still up): {exc}"
    elif onboard and repo_abs and not ready:
        onboarding = "skipped (team still booting — onboard it later or re-dispatch)"

    return {
        "team": name,
        "id": wid,
        "port": assigned,
        "a2a": f"{base}/a2a",
        "repo": repo_abs,
        "gate": gate,
        "repo_freshness": freshness,
        "board": board_mode,
        "auto_dispose": bool(auto_dispose),
        "ready": ready,
        "onboarding": onboarding,
        "next": (
            f"Send work with portfolio_dispatch(board={name!r}, title=..., spec=...). "
            f"Dispose with portfolio_teardown_team({name!r}), or it self-disposes "
            "(portfolio_autodispose) once its board drains."
            + ("" if ready else " NOTE: still booting — give it a moment before dispatching.")
        ),
    }


def _tools(cfg: dict | None = None) -> list:
    cfg = cfg or {}

    @tool
    def portfolio_boards() -> str:
        """List the team boards you can orchestrate. Each is a remote team-agent — its
        own protoAgent instance running a project board for its repo. Returns each
        board's name, url, and whether it's reachable. Use the name as the ``board``
        argument to portfolio_dispatch / portfolio_board_read."""
        from graph.fleet import supervisor

        boards = [
            {"board": a["name"], "url": a.get("url"), "reachable": bool(a.get("running"))}
            for a in supervisor.status()
            if a.get("remote") and a.get("url")
        ]
        if not boards:
            return (
                "No team boards yet. A team board is a remote protoAgent (running the "
                "project_board plugin for its repo) registered as a fleet member — add one "
                "via the console (Discover → Add to this fleet) or POST /api/fleet/remotes."
            )
        return json.dumps(boards, indent=2)

    @tool
    async def portfolio_dispatch(
        board: str,
        title: str,
        spec: str,
        acceptance_criteria: str = "",
        files_to_modify: str = "",
    ) -> str:
        """Dispatch a feature to a team board over A2A. ``board`` is a team-agent name
        (see portfolio_boards). The team's lead agent creates the feature on its OWN
        board and marks it ready; its loop then ships the PR in ITS repo. Give a
        self-sufficient spec + acceptance criteria + the files to touch — a vague task
        makes a coder produce nothing. Returns the team agent's reply."""
        rec = _remote_by_name(board)
        if rec is None:
            return f"Error: no team board named {board!r}. Call portfolio_boards to list them."
        try:
            return await _a2a_create_feature(rec, title, spec, acceptance_criteria, files_to_modify)
        except Exception as exc:  # noqa: BLE001 — surface the dispatch failure to the model
            return f"Error dispatching to {board!r}: {exc}"

    @tool
    async def portfolio_board_read(board: str, state: str = "") -> str:
        """Read a team board's current state (structured) — the bounded view a PM
        reasons over. ``board`` is a team-agent name (see portfolio_boards); optional
        ``state`` filters to one lane (backlog/ready/in_progress/in_review/done/
        blocked). Returns the features as JSON."""
        rec = _remote_by_name(board)
        if rec is None:
            return f"Error: no team board named {board!r}. Call portfolio_boards to list them."
        try:
            feats = await _fetch_board_features(rec, state)
        except _BoardUnavailable as exc:
            return f"Error reading {board!r} board: {exc}"
        return json.dumps(feats, indent=2)

    @tool
    async def portfolio_rollup(boards: str = "") -> str:
        """A BOUNDED portfolio view across team boards: per-board lane counts + only the
        blocked / critical-path (foundation) items — NOT every feature — so you can
        reason over MANY boards at once without pulling each one raw. Optional comma-
        separated ``boards`` filters to specific team-agent names (default = all). An
        unreachable board is reported with an ``error`` instead of failing the rollup."""
        wanted = {b.strip() for b in boards.split(",") if b.strip()} if boards else None
        recs = [r for r in _all_board_recs() if wanted is None or r.get("name") in wanted or r.get("id") in wanted]
        if not recs:
            return (
                "No matching team boards. Call portfolio_boards to list them."
                if wanted
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )

        async def _one(rec: dict) -> dict:
            try:
                feats = await _fetch_board_features(rec)
            except _BoardUnavailable as exc:
                return {"board": rec.get("name"), "error": str(exc)}
            return _rollup_one(rec.get("name"), feats)

        rollups = await asyncio.gather(*[_one(r) for r in recs])
        return json.dumps(rollups, indent=2)

    @tool
    async def portfolio_diff(boards: str = "") -> str:
        """Report what CHANGED on the team boards since the last check — features that
        merged (reached done), newly blocked, unblocked, or newly added — then update
        the baseline. The bounded, push-free way to stay current: schedule this (see
        portfolio_watch) and each run surfaces only the deltas. The FIRST run records a
        baseline and reports nothing (there's no 'before'). Optional comma-separated
        ``boards`` filter."""
        res = await _compute_portfolio_diff(_parse_boards(boards))
        if res["recs"] == 0:
            return (
                "No matching team boards. Call portfolio_boards to list them."
                if boards
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )
        if res["first_run"]:
            return (
                f"Baseline recorded for {res['recs']} board(s). Future portfolio_diff calls "
                "report only what changed since now."
            )
        if not res["changes"]:
            return "No board changes since the last check."
        return json.dumps(res["changes"], indent=2)

    @tool
    async def portfolio_watch(interval_min: int = 15, boards: str = "") -> str:
        """Start watching the team boards for changes WITHOUT polling: record a baseline
        now, then hand you the exact schedule_task call to run a recurring portfolio_diff.
        Each scheduled fire arrives as a turn carrying only the changes since the prior
        sweep — so the system polls for you, not your reasoning loop. Optional
        ``interval_min`` (default 15) and comma-separated ``boards`` filter."""
        res = await _compute_portfolio_diff(_parse_boards(boards))
        if res["recs"] == 0:
            return (
                "No matching team boards to watch."
                if boards
                else "No team boards yet — register a team-agent as a fleet member first (see portfolio_boards)."
            )
        interval = max(1, int(interval_min))
        cron = f"*/{interval} * * * *" if interval < 60 else "0 * * * *"
        filt = f' boards="{boards}"' if boards else ""
        return (
            f"Baseline captured for {res['recs']} board(s). To receive deltas without polling, "
            "schedule a recurring sweep with your schedule_task tool:\n\n"
            f'  schedule_task(prompt="Run portfolio_diff{filt} and report any board changes; '
            f'if there are none, do nothing.", when="{cron}")\n\n'
            "Each fire arrives as a turn carrying only the changes since the prior sweep."
        )

    @tool
    def portfolio_link(
        from_board: str = "",
        from_feature: str = "",
        to_board: str = "",
        to_feature: str = "",
        note: str = "",
        title: str = "",
        spec: str = "",
        acceptance_criteria: str = "",
        files_to_modify: str = "",
        remove: str = "",
    ) -> str:
        """Record (or remove) a CROSS-BOARD dependency: ``from_board``'s ``from_feature``
        is blocked until ``to_board``'s ``to_feature`` is done (merged on that team's
        board). Features are addressed by (board name, feature id) — ids are board-local,
        so the board is always required.

        Give ``title`` + ``spec`` (and optionally acceptance_criteria / files_to_modify)
        to make it a **planned dispatch**: ``from_feature`` is then just a planning label,
        the work is NOT created on ``from_board`` yet, and ``portfolio_autodispatch`` will
        create it there once the blocker ships. Without title/spec it's a plain advisory
        link between two existing features.

        Run portfolio_plan to see the graph + what's unblocked. To delete a link, pass
        ``remove="lnk-..."`` (the id from portfolio_plan)."""
        # Lock the whole read-modify-write so two concurrent portfolio_link calls in one
        # turn can't clobber each other's append (the dropped-link race).
        with _file_lock(_links_path()):
            links = _load_links()
            if remove:
                kept = [ln for ln in links if ln.get("id") != remove]
                if len(kept) == len(links):
                    return f"No cross-board link {remove!r} to remove."
                _save_links(kept)
                return f"Removed link {remove}."
            if not (from_board and from_feature and to_board and to_feature):
                return "Error: from_board, from_feature, to_board and to_feature are all required."
            if (from_board, from_feature) == (to_board, to_feature):
                return "Error: a feature can't depend on itself."
            for b in (from_board, to_board):
                if _remote_by_name(b) is None:
                    return f"Error: no team board named {b!r}. Call portfolio_boards to list them."
            eid = _edge_id(from_board, from_feature, to_board, to_feature)
            if any(ln.get("id") == eid for ln in links):
                return f"Already linked ({eid})."
            edge = {
                "id": eid,
                "from_board": from_board,
                "from_feature": from_feature,
                "to_board": to_board,
                "to_feature": to_feature,
                "note": note,
            }
            if title or spec:  # planned dispatch — the held work to create once unblocked
                edge.update(
                    title=title,
                    spec=spec,
                    acceptance_criteria=acceptance_criteria,
                    files_to_modify=files_to_modify,
                    dispatched=False,
                )
            if _has_cycle(links + [edge]):
                return "Error: that link would create a cross-board dependency cycle — not recorded."
            _save_links(links + [edge])
        fields = ["id", "from_board", "from_feature", "to_board", "to_feature"]
        if edge.get("spec"):
            fields += ["title", "dispatched"]
        return json.dumps({k: edge[k] for k in fields}, indent=2)

    @tool
    async def portfolio_plan() -> str:
        """The cross-board dependency graph + what's ready to dispatch next. For each link
        (see portfolio_link): ``satisfied`` (the depended-on feature is done), ``blocking``
        (not yet), ``unknown`` (its board is unreachable — never assumed satisfied), or
        ``dangling`` (the board/feature no longer exists — prune it). ``ready_to_dispatch``
        = ``from`` features whose every blocker is satisfied and that haven't started yet;
        ``blocked`` lists the rest with their open blockers."""
        plan = await _compute_plan()
        if not plan["links"]:
            return (
                "No cross-board links yet. Use portfolio_link to record a dependency, then portfolio_plan to sequence."
            )
        return json.dumps(plan, indent=2)

    @tool
    async def portfolio_autodispatch(dry_run: bool = False) -> str:
        """Close the loop: dispatch every **planned** cross-board link whose blocker has
        shipped (the to-feature reached ``done``) and that hasn't been dispatched yet —
        creating the held work on its board now that the dependency is satisfied. Set up
        the held work with ``portfolio_link(..., title=, spec=)``. Idempotent: a per-link
        ``dispatched`` flag prevents re-creating, so it's safe to run on a schedule. Pass
        ``dry_run=True`` to preview what would dispatch without doing it. Still-blocked or
        advisory-only links are left alone."""
        links = _load_links()
        pending = [ln for ln in links if ln.get("spec") and not ln.get("dispatched")]
        if not pending:
            return "No pending planned-dispatch links. Create one with portfolio_link(..., title=, spec=)."
        by_board, unreachable = await _fetch_all(_all_board_recs())
        done = {
            (name, f["id"])
            for name, feats in by_board.items()
            for f in feats
            if f.get("id") and f.get("board_state") == "done"
        }
        ready = [
            ln for ln in pending if ln["to_board"] not in unreachable and (ln["to_board"], ln["to_feature"]) in done
        ]
        if not ready:
            return "Nothing to auto-dispatch — every planned link is still blocked (or its blocker's board is unreachable)."
        if dry_run:
            return json.dumps(
                {"would_dispatch": [{"id": ln["id"], "board": ln["from_board"], "title": ln["title"]} for ln in ready]},
                indent=2,
            )
        results = []
        for ln in ready:
            rec = _remote_by_name(ln["from_board"])
            if rec is None:
                results.append({"id": ln["id"], "board": ln["from_board"], "error": "board no longer registered"})
                continue
            try:
                reply = await _a2a_create_feature(
                    rec, ln["title"], ln["spec"], ln.get("acceptance_criteria", ""), ln.get("files_to_modify", "")
                )
            except Exception as exc:  # noqa: BLE001 — report per-link; don't sink the batch
                results.append({"id": ln["id"], "board": ln["from_board"], "error": str(exc)})
                continue
            ln["dispatched"] = True  # idempotency: don't re-create on the next run
            results.append(
                {
                    "id": ln["id"],
                    "board": ln["from_board"],
                    "title": ln["title"],
                    "dispatched": True,
                    "reply": reply[:200],
                }
            )
        _save_links(links)
        return json.dumps(results, indent=2)

    @tool
    async def portfolio_spinup_team(
        name: str,
        repo: str = "",
        template: str = "",
        gate: str = "",
        port: int = 0,
        auto_dispose: bool = True,
        plugins_dir: str = "",
        onboard: bool = True,
        archetype: str = "",
        shared_board: bool = False,
    ) -> str:
        """Spin up an EPHEMERAL engineering team for a project — a finite-lifetime team-
        agent you spawn on demand, dispatch work to, and dispose when the board drains.

        The team's board is ISOLATED by default — its own scoped beads DB, not the repo's
        committed ``.beads`` — so it only works what you dispatch and never sees / resumes /
        pollutes the repo's native board. Pass ``shared_board=True`` only when the team IS
        that repo's dev team and should use the repo's own board.

        For a repo you work OFTEN, pass ``archetype=<name>`` (see portfolio_archetypes) — a
        prebuilt preset that fills repo/gate/template, so spinning up is just
        ``portfolio_spinup_team(name="content-1", archetype="protocontent")``. The repo is
        FRESHENED (git fetch + fast-forward the default branch) before the team works on it.

        It clones a base TEAM config ``template`` (a langgraph-config.yaml that carries the
        team's plugins — project_board + delegates — and its coder ladder) into a scoped
        workspace, binds ``repo`` into it (filling the ``{{REPO}}`` / ``{{TEAM_NAME}}`` /
        ``{{GATE}}`` sentinels in the template), points it at the external plugins it needs,
        starts the agent, registers it as a team board, and (by default) has the team
        ONBOARD the repo before you dispatch work — so the existing portfolio_dispatch /
        portfolio_board_read / rollup tools address it by ``name`` and its first PR ships clean.

        Args:
            name: The team name (also its fleet/A2A name + board prefix). Must be unique.
            archetype: A prebuilt team preset (portfolio_archetypes) that supplies repo +
                gate (+ template) for a frequently-worked repo. Explicit args still win.
            repo: Absolute path to the repo the team's board manages. Omit only for a
                prebuilt template that already bakes its repo in, or when using an archetype.
            template: Path to the base team langgraph-config.yaml (or its config dir; a
                sibling secrets.yaml is cloned too). Defaults to ``portfolio.team_template``
                config, then to the plugin's shipped example template — so you can omit it.
            gate: Pre-PR gate command for the team (fills ``{{GATE}}``). Empty = auto-detected
                from the repo (Python→ruff, VitePress→docs:build, Node→npm test).
            port: Bind port (0 = auto-assign the next free fleet port).
            auto_dispose: If true (default), portfolio_autodispose will tear this team
                down once its board drains. Set false for a team you'll dispose by hand.
            plugins_dir: Where the team finds its external plugins (project_board, github).
                Defaults to the PM host's own plugins dir, so a spawned team reuses what the
                host already has installed — no per-team reinstall. (delegates is builtin and
                plugin-devkit is in-tree, so those load regardless.) Override only to isolate.
            onboard: If true (default) and a repo is given, the team runs its onboard-project
                skill once before you dispatch work — scan, auto-fix hygiene, write a grounding
                doc, board readiness gaps. Best-effort; never fails the spawn. Set false to skip.

        Returns the team's name, port, A2A endpoint, repo freshness, an onboarding summary,
        and next steps.
        """
        result = await _spinup_team(
            name, repo, template, gate, port, auto_dispose, plugins_dir, onboard, archetype, shared_board, cfg=cfg
        )
        if "error" in result:
            return result["error"]
        return json.dumps(result, indent=2)

    @tool
    def portfolio_archetypes() -> str:
        """List the prebuilt team archetypes — named (repo, gate) presets for the repos this
        PM works often, ready to spin up at a moment's notice with
        ``portfolio_spinup_team(name=..., archetype=<name>)``. Configure them under
        ``portfolio.team_archetypes`` in config."""
        arch = _archetypes(cfg)
        if not arch:
            return (
                "No team archetypes configured. Add portfolio.team_archetypes: "
                "{<name>: {repo: <path>, gate: <cmd>}} to config for one-word spin-ups."
            )
        return json.dumps(
            {n: {"repo": a.get("repo", ""), "gate": a.get("gate", "")} for n, a in arch.items()}, indent=2
        )

    @tool
    async def portfolio_teams() -> str:
        """List the ephemeral teams THIS PM spawned (via portfolio_spinup_team) with their
        repo, port, auto-dispose flag, and current board drain status (active/done). Distinct
        from portfolio_boards, which lists ALL fleet members — including ones registered by
        hand, which portfolio never auto-disposes."""
        teams = _load_teams()
        if not teams:
            return "No spawned teams. Use portfolio_spinup_team(name, repo, template=...) to create one."
        out = []
        for t in teams:
            row = {
                "team": t["name"],
                "repo": t.get("repo", ""),
                "port": t.get("port"),
                "auto_dispose": bool(t.get("auto_dispose")),
                "a2a": f"http://127.0.0.1:{t.get('port')}/a2a",
            }
            rec = _remote_by_name(t["name"])
            if rec is None:
                row["status"] = "remote missing (stale entry — teardown to clear)"
            else:
                try:
                    feats = await _fetch_board_features(rec)
                    done = sum(1 for f in feats if f.get("board_state") == "done")
                    row["board"] = {
                        "total": len(feats),
                        "done": done,
                        "active": len(feats) - done,
                        "drained": len(feats) > 0 and done == len(feats),
                    }
                except _BoardUnavailable as exc:
                    row["board"] = f"unreadable ({exc})"
            out.append(row)
        return json.dumps(out, indent=2)

    @tool
    async def portfolio_teardown_team(name: str) -> str:
        """Tear down an ephemeral team this PM spawned: stop its server, purge its workspace
        + scoped data, and unregister the fleet remote. The managed repo and any PRs it
        opened are NOT touched. Only teams from portfolio_spinup_team are disposable here —
        a hand-registered remote is removed from the console, not here."""
        name = (name or "").strip()
        team = _team_by_name(name)
        if team is None:
            return (
                f"Error: {name!r} is not a portfolio-spawned team (see portfolio_teams). A manually-"
                "registered remote board is removed from the console / POST /api/fleet/remotes, not here."
            )
        out = await asyncio.to_thread(_dispose, team)
        return json.dumps(out, indent=2)

    @tool
    async def portfolio_autodispose(dry_run: bool = False) -> str:
        """Close the one-shot lifecycle: tear down every ephemeral team that's finished or
        DEAD — its board DRAINED (work dispatched, every feature done/merged), OR its
        process is gone (e.g. a host restart killed it, leaving a zombie workspace). Only
        ``auto_dispose`` teams spawned via portfolio_spinup_team are considered, and a team
        with no work yet (empty, still-running board) is NEVER disposed. Idempotent +
        schedulable: pair it with a cron (like portfolio_watch) so finite projects — and the
        debris of a restart — clean themselves up. Pass dry_run=True to preview."""
        from graph.fleet import supervisor

        teams = [t for t in _load_teams() if t.get("auto_dispose")]
        if not teams:
            return "No auto-dispose teams. Spin one up with portfolio_spinup_team(auto_dispose=True)."
        to_dispose, kept = [], []  # to_dispose: list of (team, reason)
        for t in teams:
            rec = _remote_by_name(t["name"])
            if rec is None:
                _forget_team(t["name"])  # workspace + remote both gone — prune the stale entry
                kept.append({"team": t["name"], "status": "missing — pruned"})
                continue
            try:
                feats = await _fetch_board_features(rec)
            except _BoardUnavailable as exc:
                # Unreachable: DEAD (its local process is gone — dispose the zombie
                # workspace) vs a transient hiccup on a still-running team (leave it up).
                if not supervisor.is_running(t.get("id", "")):
                    to_dispose.append((t, "dead (process gone)"))
                else:
                    kept.append({"team": t["name"], "status": f"unreadable ({exc}) — left up"})
                continue
            total = len(feats)
            done = sum(1 for f in feats if f.get("board_state") == "done")
            if total > 0 and done == total:
                to_dispose.append((t, "drained"))
            else:
                kept.append({"team": t["name"], "active": total - done, "done": done})
        if dry_run:
            return json.dumps({"would_dispose": [t["name"] for t, _ in to_dispose], "kept": kept}, indent=2)
        disposed = []
        for t, reason in to_dispose:
            out = await asyncio.to_thread(_dispose, t)
            disposed.append({**out, "reason": reason})
        return json.dumps({"disposed": disposed, "kept": kept}, indent=2)

    return [
        portfolio_boards,
        portfolio_dispatch,
        portfolio_board_read,
        portfolio_rollup,
        portfolio_diff,
        portfolio_watch,
        portfolio_link,
        portfolio_plan,
        portfolio_autodispatch,
        portfolio_spinup_team,
        portfolio_archetypes,
        portfolio_teams,
        portfolio_teardown_team,
        portfolio_autodispose,
    ]
