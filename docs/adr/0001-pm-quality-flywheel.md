# 0001 — The PM quality flywheel: cross-team learning from findings + bounce data

- Status: Proposed (design only — implementation deliberately out of scope this sprint)
- Date: 2026-07-06
- Builds on: protoAgent ADR 0006 (observability flywheel — this extends it past "advise"),
  ADR 0055 (federated boards / this plugin's rollup surface), ADR 0064 (execution-grounded
  code-solve — the `gens:` spend this reads), ADR 0077 (the findings convention this
  aggregates); projectBoard-plugin v0.30.0–0.31.0 (the review/design gates that now
  *produce* the data).
- Plan: protoAgent [`docs/plans/codified-delivery-loop.md`](https://github.com/protoLabsAI/protoAgent/blob/main/docs/plans/codified-delivery-loop.md) (M8).

## Context

Until this sprint the PM tier could see *flow* (lane counts, blocked, critical path,
stuck) but not *quality*: why work bounced, what reviews caught, where the ladder burned
tiers. The delivery-loop program changed what exists on every team board:

- **Review findings** in the ADR 0077 schema, stored as bead comments by the board's
  blocking review gate (`set_review_substate`), with `review-pending` /
  `changes-requested` labels marking the gate's sub-states.
- **Bounce counters** for every corrective loop: CI (`ci_fix_max`), review
  (`review_fix_max`), rebase, goal-verify, local-gate — each exhaustion recorded as a
  distinct blocked reason.
- **Ladder telemetry**: `tier:` / `attempt:` labels, `gens:` spend from `coder.solve()`,
  escalation comments.
- **Design-gate rejections**: `mark_ready` refusals for undesigned large/architectural
  work (the error text names the missing artifact).

Each team's retro already mines its OWN board (the ADR 0006 loop: retro grounds → coder
heeds). What no tier does is learn **across** teams: the same finding category recurring
on three boards is a *process* defect (a template gap, a missing lesson, a wrong default),
invisible to any single team.

## Decision

**D1 — The rollup grows a bounded `quality` block, computed board-side.** Extend the
board API's rollup projection (not the PM's context) with per-board aggregates: bounce
counts by loop (ci/review/rebase/goal/gate), finding counts by category × severity
(parsed from the gate's comments at write time, not re-parsed later), escalation count +
top rung reached, design-gate rejections. Raw findings stay on the beads; the PM reads
AGGREGATES ONLY (the `_rollup_one` boundedness rule — a PM's context must not scale with
team count × finding count).

**D2 — The PM's flywheel loop is a scheduled pass, not a reactive hook.** A
`portfolio_retro` pass (cadence: weekly, or on demand) that: reads the quality blocks
across boards → diffs against the previous pass → identifies cross-team patterns
(same category ≥ N boards, a team's bounce rate ≥ k× fleet median, ladder burn on a
difficulty class) → produces exactly three kinds of output:

1. **Dispatch policy** (the PM's own behavior): route work classes to teams with the
   better track record on them; require `designing` + due diligence at a lower
   difficulty threshold on teams with high review-bounce rates; tune `review_fix_max` /
   `max_mode_n` recommendations per team.
2. **Process advice** (a memo to the team lead over A2A): "conventions findings dominate
   your bounces — adopt the repo's format_cmd/local_gate_cmd" — advice, never a config
   write into the team.
3. **Lessons** (`memory_ingest` into the PM's KB + a suggested lesson for the TEAM's
   knowledge graph): the cross-team gotcha phrased so the team-side flywheel (the
   `_fetch_kg_lessons` read in the board loop) can inject it into coder prompts.

**D3 — Humans stay the actuator for policy that crosses a boundary.** The PM applies
D2.1 unilaterally only where it already owns the knob (its own dispatch). Anything that
changes a TEAM's config (gate budgets, thresholds) ships as a recommendation the operator
or team lead applies — same posture as the github plugin's confirm-guarded merge.

**D4 — No new store.** The flywheel state (previous pass's aggregates, advice sent) lives
in the PM's existing memory/KB, keyed by board name — not a parallel database that can
drift from the boards (the anti-82-phantom rule this stack already lives by).

## Consequences

- The board API grows one read-only projection; teams pay nothing at runtime (aggregation
  happens where the comments already live).
- The findings convention becomes load-bearing across THREE tiers (coder prompt ← board
  gate ← PM rollup) — schema changes to ADR 0077 now need a compat note here.
- The advice loop is only as good as category hygiene in findings; if finders drift into
  free-text categories, D1's aggregates silt up — the review-synthesizer's category
  normalization is the upstream guard.
- Deliberately NOT built yet: implementation is scoped for a later sprint, after M5's
  gate has produced enough real findings data on live boards to validate the aggregate
  shapes against (the data must exist before the dashboard for it).
- Revisit triggers: first live sprint with `review_gate: true` fleet-wide; or the
  findings volume making bead-comment parsing at rollup time noticeably slow (then
  aggregate at gate-write time into labels instead).
