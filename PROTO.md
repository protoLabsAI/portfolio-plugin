# PROTO.md — portfolio plugin

Agent-instructions grounding doc for coding agents working on this repo.
If you're not sure what convention to follow, read this file first.

## Stack

- **Language:** Python 3.11+ (declared in `pyproject.toml` `requires-python`)
- **Shape:** Pure composition plugin for [protoAgent](https://github.com/proto-agent/protoAgent) — **no runtime pip deps**.
  At runtime langchain-core, FastAPI, httpx, pyyaml, filelock, and the fleet / delegates / project_board host
  seams are all provided by the protoAgent host. This plugin adds zero `pip` dependencies.
- **Tool surface:** `langchain_core.tools.tool`-decorated functions registered via `register(registry)`.
- **Console view:** FastAPI routers (`build_view_router`, `build_data_router`) mounted at
  `/plugins/portfolio` (public HTML page) and `/api/plugins/portfolio` (bearer-gated data).
- **Host seams:** `graph.fleet.supervisor`, `graph.workspaces.manager`, `graph.config_io`, `runtime.state`,
  `graph.plugins.installer`, `plugins.delegates.adapters`, `infra.paths`, `security.policy` — all lazy-imported
  inside function bodies so the module imports cleanly without the host present.

## Build / run / test

```bash
pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q
```

- `ruff check` — lint (E/F/W, line-length 120, pyproject-configured ignores)
- `ruff format --check` — format (120-char lines, py311 target)
- `pytest -q` — test suite (asyncio_mode=auto, runs standalone against the conftest stubs)

If anything in this sequence fails, fix it before committing.

## Conventions

- **Line length:** 120 (ruff `line-length = 120`).
- **Async:** `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`. All async tests use plain
  `async def test_xxx` — no `@pytest.mark.asyncio` needed.
- **Acceptance criteria:** When implementing a feature, write EARS-style acceptance criteria
  (e.g. "Given X, when Y, then Z") in the docstring or a nearby comment block. The test file
  should exercise each criterion.
- **Version lockstep:** `pyproject.toml` `[project].version` and `protoagent.plugin.yaml` `version`
  MUST agree. This is enforced by `tests/test_packaging.py::test_manifest_and_pyproject_versions_agree`.
  When bumping one, bump the other.
- **Min host version:** `protoagent.plugin.yaml` `min_protoagent_version` reflects the oldest
  protoAgent release whose seams this plugin depends on. Bump only when a new seam is added.
- **Ships disabled:** `protoagent.plugin.yaml` `enabled: false`. Enabling on the PM is the
  trust decision — never flip this in a feature branch.

## Where shared deps / assets live

- **Host seams stubbed for tests:** `tests/conftest.py`. This is the single source of truth for
  how the plugin's host dependencies are faked. It registers lightweight stubs for `graph.fleet`,
  `graph.workspaces`, `graph.config_io`, `runtime.state`, `graph.plugins.installer`,
  `plugins.delegates.adapters`, `infra.paths`, and `security.policy`. **Use these stubs — never
  fabricate lookalikes in individual test files.** The real implementations are provided by
  protoAgent at runtime.
- **Test suite runs standalone.** No protoAgent, no HTTP server, no real fleet — just the
  conftest stubs and `monkeypatch`. CI runs `pytest -q` with no other setup.
- **Example templates:** `examples/team-template/` is the shipped default for
  `portfolio_spinup_team` when no `team_template` config is set.
- **Data paths:** All PM state files (snapshot, links, teams) resolve via `_store_path(name)`
  which prefers `infra.paths.instance_paths().store()` (protoAgent >= 0.77) and falls back to
  `scope_leaf(data_home() / name)` for older hosts.

## Do / Don't

| Do | Don't |
|----|-------|
| Keep `pyproject.toml` and `protoagent.plugin.yaml` versions in lockstep (guarded by `test_packaging.py`) | Add runtime pip deps — this plugin is pure composition |
| Use the real stubs in `tests/conftest.py` | Fabricate your own lookalike host seams in tests |
| Lazy-import host packages inside function bodies | Top-level import host packages (the module must import without protoAgent) |
| Write EARS-style acceptance criteria for new features | Ship a feature without tests covering its criteria |
| Use `_store_path()` for all PM data files | Hard-code paths or use `Path.home()` directly |
| Bump `min_protoagent_version` only when a new seam is required | Bump it to avoid a real fix |
| Exclude only per-session `.proto/` scratch (`.proto/memory/`, `.proto/session-notes.md`, `.proto/repo-map-cache.json`) via `.git/info/exclude` | Blanket-ignore `.proto/` — that kills `protoCLI` skills under `.proto/evolve` |
| Handle concurrent tool calls with `_file_lock()` for RMW on shared files (links, teams) | Assume single-threaded tool calls |

## Architecture notes

- **Pure composition:** The PM tool surface is built by composing three existing subsystems —
  fleet (team-agent registry), delegates (A2A dispatch), and project_board (remote board read).
  No new dispatch or registry machinery.
- **P2 pull-diff:** Board state deltas use snapshot + diff (PM-side), not push notifications.
  `portfolio_watch` + `portfolio_diff` baseline then report changes on a schedule.
- **P3 cross-board graph:** `portfolio_link` / `portfolio_plan` / `portfolio_autodispatch` form
  a dependency graph. Cycles are rejected at link time. Autodispatch is idempotent.
- **Ephemeral teams:** `portfolio_spinup_team` / `teardown_team` / `autodispose` manage finite-lifetime
  teams. Spinup clones a template, binds the repo, starts the agent, registers it. Autodispose
  only touches `auto_dispose=True` teams whose boards drain.
