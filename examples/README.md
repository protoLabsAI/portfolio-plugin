# Team templates for `portfolio_spinup_team`

A **team template** is a base `langgraph-config.yaml` (+ a sibling `secrets.yaml`) that
`portfolio_spinup_team` clones into a scoped workspace and fills per spawn. Point
`portfolio.team_template` at one, or pass `template=` per call.

| Template | What it spawns |
|---|---|
| [`team-template/`](team-template/) | A generic ephemeral engineering team — `project_board` + `delegates`, repo filled per spawn |
| [`plugin-maker-team/`](plugin-maker-team/) | A **plugin shop** — adds `plugin-devkit` (scaffold + the `building-plugins` skill + `plugin-architect`) and `github`, to build net-new protoAgent plugins |

## Per-spawn sentinels

`portfolio_spinup_team` fills these in the cloned config (plain string replace, so the
template's comments survive):

| Sentinel | Filled with |
|---|---|
| `{{REPO}}` | the `repo` argument — the repo the team's board manages |
| `{{TEAM_NAME}}` | the `name` argument |
| `{{GATE}}` | the `gate` argument (pre-PR check command; empty = none) |

## Where the team's plugins come from

- `delegates` is **builtin** and `plugin-devkit` is **in-tree** — both load in any
  workspace for free.
- External plugins (`project_board`, `github`) need a discovery root. `portfolio_spinup_team`
  defaults the spawned team's `plugins.dir` to the **PM host's own plugins dir**, so the team
  reuses what the host already has installed — no per-team reinstall. Override with the
  `plugins_dir` arg or `portfolio.team_plugins_dir` config, or bake a `plugins.dir` into the
  template (it's respected if present).
- `secrets.yaml` (next to the template's `langgraph-config.yaml`) is cloned too — copy the
  `secrets.example.yaml` and fill in the gateway key.

## Prebuilt repo-teams (long-running repos)

For a repo you spin up for again and again, make a **prebuilt template**: copy
`team-template/`, replace `{{REPO}}` with the repo's real path (and `{{GATE}}` with its
check command), and keep `{{TEAM_NAME}}` as-is. Now spin it up by name with **no `repo`
argument**:

```
portfolio_spinup_team(name="protolibrary-team", template=".../templates/protolibrary-team")
```

The team boots already pointed at the repo — and because it manages that repo through the
fenced filesystem, it reads the repo's own in-repo grounding (`PROTO.md`) as context. Add
repo-specific operating notes to the template (e.g. the gate, the coder ladder, extra
`filesystem.projects`) so the team comes packaged with everything it needs for that repo.
