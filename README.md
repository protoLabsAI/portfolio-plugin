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
