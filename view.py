"""portfolio console view (ADR 0026) — a Portfolio dashboard.

One card per team/board the PM orchestrates: lane counts (a mini board summary),
blocked + critical-path items, drain status, whether it's a portfolio-spawned ephemeral
team (auto-dispose) or a standing member, and its A2A endpoint. Pure read — it reflects
the SAME rollup the portfolio_rollup tool computes (``_fetch_board_features`` ⊕
``_rollup_one``), so the panel and the agent see one truth.

Two routers (the view contract): the PAGE on the PUBLIC ``/plugins/portfolio`` prefix
(an iframe src can't carry a bearer), the DATA on the GATED ``/api/plugins/portfolio``.
The page derives its base from its own path so it works through the fleet proxy, and
loads the DS plugin-kit for theme + slug-aware authed fetch.
"""

from __future__ import annotations

VIEW_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio</title>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0b0d10);color:var(--pl-color-fg,#e6e6e6);
    font-family:var(--pl-font-sans,system-ui,sans-serif);font-size:13px}
  .wrap{max-width:1240px;margin:0 auto;padding:var(--pl-space-4,16px) var(--pl-space-6,24px)}
  header{display:flex;align-items:baseline;gap:var(--pl-space-4,16px);margin-bottom:var(--pl-space-4,16px)}
  header h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--pl-color-fg-muted,#8a8f98);font-size:12px}
  .sub b{color:var(--pl-color-fg,#e6e6e6);font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:var(--pl-space-3,12px)}
  .card{background:var(--pl-color-bg-subtle,#15181d);border:1px solid var(--pl-color-border,#272b33);
    border-radius:var(--pl-radius-md,8px);padding:var(--pl-space-3,12px) var(--pl-space-4,16px)}
  .card.dim{opacity:.55}
  .chead{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .name{font-weight:600;font-size:14px}
  .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:var(--pl-color-fg-muted,#8a8f98)}
  .dot.up{background:var(--pl-color-success,#3fb950)} .dot.down{background:var(--pl-color-danger,#f85149)}
  .badge{font-size:10px;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;border-radius:999px;
    border:1px solid var(--pl-color-border,#272b33);color:var(--pl-color-fg-muted,#8a8f98)}
  .badge.eph{color:var(--pl-color-accent,#6cb6ff);border-color:var(--pl-color-accent,#6cb6ff)}
  .repo{font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11px;color:var(--pl-color-fg-muted,#8a8f98);
    margin:2px 0 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .lanes{display:flex;gap:6px;margin-bottom:8px}
  .lane{flex:1;text-align:center;background:var(--pl-color-bg,#0b0d10);border-radius:var(--pl-radius-sm,4px);padding:4px 2px}
  .lane .n{font-size:15px;font-weight:600;line-height:1.1}
  .lane .l{font-size:9px;text-transform:uppercase;letter-spacing:.03em;color:var(--pl-color-fg-muted,#8a8f98)}
  .lane.done .n{color:var(--pl-color-success,#3fb950)} .lane.prog .n{color:var(--pl-color-accent,#6cb6ff)}
  .foot{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px;
    color:var(--pl-color-fg-muted,#8a8f98)}
  .pill{padding:1px 7px;border-radius:999px;background:var(--pl-color-bg,#0b0d10);border:1px solid var(--pl-color-border,#272b33)}
  .pill.blk{color:var(--pl-color-danger,#f85149);border-color:var(--pl-color-danger,#f85149)}
  .pill.drn{color:var(--pl-color-success,#3fb950);border-color:var(--pl-color-success,#3fb950)}
  .a2a{font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:10px;opacity:.7;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:48%}
  .err{color:var(--pl-color-danger,#f85149);font-size:11px;margin-top:4px}
  .empty{color:var(--pl-color-fg-muted,#8a8f98);padding:48px 0;text-align:center}
  .empty code{font-family:var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-fg,#e6e6e6)}
</style>
<script>
  // Slug-aware base (so assets + fetches stay same-origin through the fleet proxy).
  var BASE = location.pathname.split("/plugins/")[0];
  (function(){ var l=document.createElement("link"); l.rel="stylesheet";
    l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
</head><body><div class="wrap">
  <header>
    <h1>Portfolio</h1>
    <div class="sub" id="summary"></div>
  </header>
  <div id="err" class="err" hidden></div>
  <div id="grid" class="grid"></div>
</div>
<script type="module">
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }

const LANES = [["backlog","backlog"],["ready","ready"],["in_progress","prog"],["in_review","review"],["done","done"]];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function card(b){
  const reach = b.reachable;
  const counts = b.counts || {};
  const lanes = LANES.map(([k,cls]) =>
    `<div class="lane ${cls}"><div class="n">${counts[k]||0}</div><div class="l">${k.replace("_"," ")}</div></div>`
  ).join("");
  const blocked = (b.blocked||[]).length;
  const badge = b.spawned ? `<span class="badge eph">ephemeral</span>` : `<span class="badge">standing</span>`;
  const drained = b.drained ? `<span class="pill drn">drained</span>`
    : reach ? `<span class="pill">${(b.total||0) - (counts.done||0)} active</span>` : "";
  const blk = blocked ? `<span class="pill blk">${blocked} blocked</span>` : "";
  const body = reach
    ? `<div class="lanes">${lanes}</div>
       <div class="foot"><span>${blk}${blk?" ":""}${drained}</span>
         <span class="a2a">${esc(b.a2a||"")}</span></div>`
    : `<div class="err">${esc(b.error||"unreachable")}</div>`;
  return `<div class="card ${reach?"":"dim"}">
    <div class="chead"><span class="dot ${reach?"up":"down"}"></span>
      <span class="name">${esc(b.board)}</span>${badge}</div>
    ${b.repo ? `<div class="repo">${esc(b.repo)}</div>` : ""}
    ${body}</div>`;
}

async function load(){
  try {
    const data = await (await kit.apiFetch("/api/plugins/portfolio/overview")).json();
    const boards = data.boards || [];
    document.getElementById("err").hidden = true;
    if (!boards.length){
      document.getElementById("grid").innerHTML =
        `<div class="empty">No teams yet. Ask the portfolio manager to <code>spinup_team(name, repo)</code>.</div>`;
      document.getElementById("summary").textContent = "";
      return;
    }
    const active = boards.reduce((a,b)=>a + ((b.total||0)-((b.counts||{}).done||0)), 0);
    const blocked = boards.reduce((a,b)=>a + (b.blocked||[]).length, 0);
    document.getElementById("summary").innerHTML =
      `<b>${boards.length}</b> board${boards.length>1?"s":""} · <b>${active}</b> active · <b>${blocked}</b> blocked`;
    document.getElementById("grid").innerHTML = boards.map(card).join("");
  } catch (e) {
    document.getElementById("err").hidden = false;
    document.getElementById("err").textContent = "Could not load the portfolio: " + e;
  }
}

let booted = false;
function boot(){ if (booted) return; booted = true; load(); setInterval(load, 5000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
</script></body></html>"""


def build_view_router():
    """The PUBLIC page router (mounted at /plugins/portfolio)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/dashboard")
    async def _dashboard() -> HTMLResponse:  # served at /plugins/portfolio/dashboard
        return HTMLResponse(VIEW_PAGE)

    return router


def build_data_router():
    """The GATED data router (mounted at /api/plugins/portfolio). Reflects the same
    rollup the portfolio_rollup tool computes — one card per team/board."""
    import asyncio

    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/overview")
    async def _overview() -> dict:
        # Lazy + relative: the package's __init__ is fully loaded by request time.
        from . import _BoardUnavailable, _fetch_board_features, _load_teams, _remote_by_name, _rollup_one
        from graph.fleet import supervisor

        teams = {t["name"]: t for t in _load_teams()}
        # Candidate boards: portfolio-spawned teams + every non-host fleet member.
        names: list[str] = list(teams.keys())
        try:
            for m in supervisor.status():
                if m.get("host"):
                    continue
                n = m.get("name")
                if n and n not in teams:
                    names.append(n)
        except Exception:  # noqa: BLE001 — a status hiccup shouldn't blank the panel
            pass
        seen: set = set()
        names = [n for n in names if not (n in seen or seen.add(n))]

        async def _one(name: str) -> dict:
            t = teams.get(name)
            rec = _remote_by_name(name)
            a2a = (
                f"http://127.0.0.1:{t['port']}/a2a"
                if t and t.get("port")
                else (rec or {}).get("url", "") + ("/a2a" if rec else "")
            )
            base = {
                "board": name,
                "spawned": name in teams,
                "repo": (t or {}).get("repo", ""),
                "auto_dispose": bool((t or {}).get("auto_dispose")),
                "a2a": a2a,
            }
            if rec is None:
                return {**base, "reachable": False, "error": "not resolvable"}
            try:
                feats = await _fetch_board_features(rec)
            except _BoardUnavailable as exc:
                return {**base, "reachable": False, "error": str(exc)}
            roll = _rollup_one(name, feats)
            done = sum(1 for f in feats if f.get("board_state") == "done")
            return {**base, **roll, "reachable": True, "drained": len(feats) > 0 and done == len(feats)}

        boards = list(await asyncio.gather(*[_one(n) for n in names])) if names else []
        return {"boards": boards}

    return router
