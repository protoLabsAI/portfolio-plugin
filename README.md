# portfolio — multi-team orchestration for protoAgent

One **PM (program-manager) agent** that orchestrates work across **many team-agents**,
each running its own [`project_board`](https://github.com/protoLabsAI/projectBoard-plugin)
for its own repo. The PM dispatches features to a team's board, rolls up state across
boards, watches for changes, and sequences cross-board dependencies — all over **A2A**,
on the protoAgent **fleet** spine. This is the *scale-out* model: multiplicity lives in
the fleet, not inside one board (see protoAgent **ADR 0055**).

**Pure composition** — no new dispatch or registry machinery: the **fleet** (the
team-agent registry), **delegates** (the A2A dispatch primitive), and **project_board**
(the board read). Ships **disabled**; enable it on the PM agent.

## Tools

| Tool | Does |
|---|---|
| `portfolio_boards()` | List the team boards (remote fleet members) |
| `portfolio_dispatch(board, title, spec, …)` | Send a feature to a team board over A2A — its lead creates + readies it on its own board |
| `portfolio_board_read(board[, state])` | Structured read of one team board |
| `portfolio_rollup([boards])` | Bounded cross-board view — per-board lane counts + only blocked / critical-path items |
| `portfolio_diff([boards])` | What changed since the last check — merged / newly-blocked / unblocked / new |
| `portfolio_watch([interval_min, boards])` | Baseline now, then the `schedule_task` cron to run `portfolio_diff` on a schedule |
| `portfolio_link(from_board, from_feature, to_board, to_feature[, note, title, spec, …, remove])` | Record (or remove) a cross-board dependency; with `title`+`spec` it's a *planned dispatch* (held work) |
| `portfolio_plan()` | The cross-board dependency graph + what's ready to dispatch next |
| `portfolio_autodispatch([dry_run])` | Create each planned link's held work once its blocker ships — idempotent, schedulable |
| `portfolio_spinup_team(name, repo[, template, gate, port, auto_dispose])` | **Spin up an ephemeral team** — clone a base team config into a scoped workspace, bind the repo, start + register it as a board |
| `portfolio_teams()` | List the teams this PM spawned + each one's board drain status |
| `portfolio_teardown_team(name)` | Stop + purge a spawned team (workspace + scoped data); the repo + its PRs are untouched |
| `portfolio_autodispose([dry_run])` | Tear down every spawned team whose board has **drained** (all work done) — the one-shot lifecycle, schedulable |

## Ephemeral teams (spin up on demand)

A PM doesn't just dispatch to *standing* teams — it can **spawn a finite-lifetime team
for a project, dispatch work to it, and dispose it when the board drains**. This is the
in-process counterpart of the `team-up.sh` / `team-down.sh` scripts: same primitives
(`graph.workspaces.manager` + `graph.fleet.supervisor`), driven by a tool the agent calls.

```
portfolio_spinup_team(name="docs-team", repo="/Users/me/dev/protoLibrary", gate="npm run docs:build")
  → clones the team template into a scoped workspace, binds the repo, starts the agent,
    registers it as a board, and returns its A2A endpoint.
portfolio_dispatch(board="docs-team", title=…, spec=…)    # send it work
portfolio_autodispose()                                   # once its board drains, it's torn down
```

**The team template.** `portfolio_spinup_team` clones a base team `langgraph-config.yaml`
(the team's plugins — `project_board` + `delegates` — and its coder ladder), filling these
per-spawn sentinels — comment-preserving, so the template stays readable:

| Sentinel | Filled with |
|---|---|
| `{{REPO}}` | the `repo` argument (omit `repo` to keep a prebuilt template's baked-in repo) |
| `{{TEAM_NAME}}` | the `name` argument |
| `{{GATE}}` | the `gate` argument (the pre-PR check command; empty = none) |

Point `portfolio.team_template` at it (or pass `template=` per call):

```yaml
# langgraph-config.yaml — on the PM agent
plugins:
  enabled: [delegates, portfolio]
portfolio:
  team_template: /Users/me/dev/portfolio-plugin/examples/team-template
```

**Where the team's plugins come from.** `delegates` (builtin) and `plugin-devkit` (in-tree)
load in any workspace for free; external plugins (`project_board`, `github`) need a
discovery root, so the spawned team's `plugins.dir` defaults to the **PM host's own plugins
dir** — it reuses what the host already has installed, no per-team reinstall. Override with
`plugins_dir=` / `portfolio.team_plugins_dir`, or bake `plugins.dir` into the template.

**Ready-to-copy templates** live in [`examples/`](examples/) — a generic
[`team-template/`](examples/team-template/) and a
[`plugin-maker-team/`](examples/plugin-maker-team/) (a plugin shop: `plugin-devkit` +
`github` + `project_board`, to build net-new protoAgent plugins). **Prebuilt repo-teams**
for a long-running repo are just a template with the repo baked in (no `{{REPO}}`) — spin
one up by name with no `repo` argument, and it boots already pointed at the repo (reading
its in-repo `PROTO.md` grounding). See [`examples/README.md`](examples/README.md).

**Auto-dispose** only ever touches teams *this PM spawned* with `auto_dispose=True`, and
never an empty board (a team with no work yet) — so a hand-registered standing team and a
just-spawned team are both safe.

## Install

Bundled with the **pm-stack** (Project Manager) bundle alongside `project_board`, or
install directly:

```yaml
# langgraph-config.yaml — on the PM agent
plugins:
  enabled: [delegates, portfolio]
```

Each **team** is its own protoAgent instance running `project_board` for its repo;
register it on the PM as a fleet member (Discover → *Add to this fleet*, or
`POST /api/fleet/remotes`). The board is addressed by that member's name. The stored
remote bearer authenticates both the team's `/a2a` (dispatch) and its board API (read).

See the protoAgent guide **`docs/guides/portfolio.md`** for the full walkthrough.

## Develop

```
pip install -r requirements-dev.txt
pytest -q          # host-free: the fleet / delegates / project_board / infra seams are stubbed in conftest
ruff check . && ruff format --check .
```

No runtime pip deps — `langchain-core` and the host seams come from protoAgent.
Keep `protoagent.plugin.yaml` and `pyproject.toml` versions in lockstep with the
release tag (guarded by `tests/test_packaging.py`).
