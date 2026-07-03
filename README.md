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
(the team's plugins — `project_board` + `delegates` + `coder` — and its model-tier coders
ladder), filling these per-spawn sentinels — comment-preserving, so the template stays
readable:

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

## Team template

The shipped [`examples/team-template/`](examples/team-template/) is the default
`langgraph-config.yaml` `portfolio_spinup_team` clones when you don't point it at your own
(see [`examples/README.md`](examples/README.md) for the general templating mechanics). This
is a field-by-field reference for what's in it and why, so you can copy it and know what
every line does.

### The `{{...}}` sentinels

Plain string replace over the cloned config, so the template's own comments survive:

| Sentinel | Filled with |
|---|---|
| `{{REPO}}` | the `repo` argument — the repo this team's board manages and its coders branch worktrees off |
| `{{TEAM_NAME}}` | the `name` argument — the team's `identity.name` and its `filesystem.projects` label |
| `{{GATE}}` | the `gate` argument — the pre-PR check command (`project_board.local_gate_cmd`); empty = no gate |

### `identity` + `model` (gateway-inherit)

```yaml
identity:
  name: "{{TEAM_NAME}}"

model:
  provider: openai
  name: protolabs/reasoning
  api_base: ""        # blank → inherits the PM host's gateway
  api_key: ""          # blank → the PM's OPENAI_API_KEY reaches the team via its environment
  temperature: 0.2
```

Leave `model.api_base` **blank** (as shipped) and `portfolio_spinup_team` fills it from the
PM host's own resolved gateway (v0.14+) — so a spawned team boots ready-to-think with **no
creds prep**. The host's key rides into the team's process environment too, so an env-only
`OPENAI_API_KEY` reaches it. Only set `api_base` (and drop a `secrets.yaml` next to the
template) when you want *this* team on a **different** gateway than the PM's.

### `agent_runtime`

`native` runs the team's brain on the `model:` above (the gateway model). Set it to
`acp:claude` to give the team an Opus brain instead — requires the `claude` CLI on PATH and
`ANTHROPIC_MODEL=opus` in the launch environment.

### `delegates` — the two supported ACP coders

```yaml
delegates:
  - { name: proto, type: acp, command: proto, args: ["--acp"], workdir: "{{REPO}}", permissions: auto }
  - { name: claude, type: acp, command: claude-code, workdir: "{{REPO}}", permissions: auto }
```

These are the coding agents the board's loop dispatches to over ACP (ADR 0024/0025), each
confined to a `workdir` bound to `{{REPO}}`:

- **`proto`** — the `proto` CLI, launched with `--acp`.
- **`claude`** — Claude Code, launched as `claude-code` — an adapter **alias** for the
  `claude-agent-acp` binary (`npm i -g @agentclientprotocol/claude-agent-acp`). It takes
  **no launch args** (unlike `proto`, it doesn't need `--acp`).

Declaring both means the `coders` ladder below (and the `coder` plugin's search ladder) has
somewhere to escalate to — a capability failure on one coder can climb to the other.

### `project_board` — the coders ladder + tier-escalation

```yaml
project_board:
  repo: "{{REPO}}"
  coders:
    smart: proto
    reasoning: claude
  loop_enabled: true
  local_gate_cmd: "{{GATE}}"
```

- **`repo`** — the repo this board manages; every feature's disposable `git worktree`
  branches off it.
- **`coders`** — a **model-tier escalation ladder** over the delegates declared above,
  cheapest-first (`smart` → `reasoning`). The board's loop dispatches the top ready feature
  to `smart` (`proto`); if that coder makes **no diff / times out** (a capability failure,
  not a test failure), the loop climbs to the next tier (`reasoning` / `claude`) and retries.
  It escalates by throwing a bigger brain at the problem, not by searching harder.
- **`loop_enabled: true`** — the board auto-dispatches ready features; an ephemeral team
  should just run without a human pulling the trigger.
- **`local_gate_cmd`** — the repo's real pre-PR check command; a coder's branch must pass it
  before a PR opens.

### `coder` — the search-ladder escalation (ADR 0064)

```yaml
coder:
  delegate: proto
```

Where `project_board.coders` escalates by **model tier**, the `coder` plugin escalates by
**search depth on a fixed model** — the two axes compose. When a feature's EARS acceptance
criteria compile to tests and the coders above still can't pass them, the board can reach
for `coder`'s difficulty-gated ladder: **greedy** (one shot) → **best-of-k** (k candidates,
execution-selected) → **tree-search** (refine on the *failing* tests, bounded depth) →
**fusion** (opt-in, richest generator). Every rung is gated on tests actually passing, never
an LLM judge — it's the missing execution-verification rung in the board loop, reserved for
genuinely **hard, verifiable** features the cheaper coders above failed.

`coder` runs **model-authored code in a subprocess** — isolation, **not** a true sandbox
(the same caveat as `execute_code`) — so only enable it for a trusted model/host. See
[ADR 0064](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0064-coder-execution-grounded-code-solve.md)
for the full ladder, the verifier contract, and the board-seam design.

### `filesystem` — fenced to the repo

```yaml
filesystem:
  enabled: true
  allow_run: false
  projects:
    - { name: "{{TEAM_NAME}}", path: "{{REPO}}", write: true }
```

The fenced filesystem (ADR 0007) scopes the team to exactly one project: its own repo, with
write access (for planning/grounding docs, not just code — the coders write code via their
own ACP workdir). `allow_run` stays `false` so a coder can never stall the loop on a HITL
shell-approval prompt.

### See also

- [ADR 0055](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0055-multi-team-orchestration-federated-boards.md) —
  multi-team orchestration (why teams are separate protoAgent instances federated over A2A,
  and how the PM addresses them as boards).
- [ADR 0064](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0064-coder-execution-grounded-code-solve.md) —
  the `coder` execution-grounded search ladder and the board seam it wires into.
- [`examples/README.md`](examples/README.md) — the general template mechanics (sentinels,
  where a spawned team's plugins come from, prebuilt repo-teams).

## Console view

The plugin ships a **Portfolio dashboard** (left-rail *Portfolio* panel) — one card per
team/board with its lane counts (backlog → done), blocked / critical-path items, drain
status, an `ephemeral` vs `standing` badge, and the A2A endpoint. It reflects the *same*
rollup the `portfolio_rollup` tool computes (so the panel and the agent see one truth) and
refreshes every few seconds. Page on the public `/plugins/portfolio/dashboard`; data on the
gated `/api/plugins/portfolio/overview`.

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
