"""portfolio console view (ADR 0026) — a Portfolio dashboard.

Two tabs:
  - **Boards** — one card per team/board (lane counts, blocked/critical-path, drain
    status, ephemeral-vs-standing, A2A). Click a card to drill into its FEATURE LIST.
  - **Dependencies** — the cross-board dependency GRAPH (the same plan portfolio_plan
    computes): nodes are board·feature, edges flow blocker → dependent, colored by status,
    plus the ready-to-dispatch / blocked summary.

Pure read — it reflects the SAME data the tools compute (``_fetch_board_features`` /
``_rollup_one`` / ``_compute_plan``), so panel and agent see one truth.

Two routers (the view contract): the PAGE on the PUBLIC ``/plugins/portfolio`` prefix (an
iframe src can't carry a bearer), the DATA on the GATED ``/api/plugins/portfolio``.
"""

from __future__ import annotations

VIEW_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio</title>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0b0d10);color:var(--pl-color-fg,#e6e6e6);
    font-family:var(--pl-font-sans,system-ui,sans-serif);font-size:13px}
  .wrap{max-width:1240px;margin:0 auto;padding:var(--pl-space-4,16px) var(--pl-space-6,24px)}
  header{display:flex;align-items:baseline;gap:var(--pl-space-4,16px);margin-bottom:10px}
  header h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--pl-color-fg-muted,#8a8f98);font-size:12px}
  .sub b{color:var(--pl-color-fg,#e6e6e6);font-weight:600}
  .tabs{display:flex;gap:4px;margin-bottom:var(--pl-space-4,16px);border-bottom:1px solid var(--pl-color-border,#272b33)}
  .tab{padding:6px 12px;cursor:pointer;color:var(--pl-color-fg-muted,#8a8f98);border-bottom:2px solid transparent;font-size:12px}
  .tab.on{color:var(--pl-color-fg,#e6e6e6);border-bottom-color:var(--pl-color-accent,#6cb6ff)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:var(--pl-space-3,12px)}
  .card{background:var(--pl-color-bg-subtle,#15181d);border:1px solid var(--pl-color-border,#272b33);
    border-radius:var(--pl-radius-md,8px);padding:var(--pl-space-3,12px) var(--pl-space-4,16px);cursor:pointer}
  .card:hover{border-color:var(--pl-color-accent,#6cb6ff)} .card.dim{opacity:.55;cursor:default}
  .card.dim:hover{border-color:var(--pl-color-border,#272b33)}
  .chead{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .name{font-weight:600;font-size:14px} .grow{flex:1}
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
  .foot{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px;color:var(--pl-color-fg-muted,#8a8f98)}
  .pill{padding:1px 7px;border-radius:999px;background:var(--pl-color-bg,#0b0d10);border:1px solid var(--pl-color-border,#272b33)}
  .pill.blk{color:var(--pl-color-danger,#f85149);border-color:var(--pl-color-danger,#f85149)}
  .pill.drn{color:var(--pl-color-success,#3fb950);border-color:var(--pl-color-success,#3fb950)}
  .a2a{font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:10px;opacity:.7;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;max-width:48%}
  .err{color:var(--pl-color-danger,#f85149);font-size:11px;margin-top:4px}
  .empty{color:var(--pl-color-fg-muted,#8a8f98);padding:48px 0;text-align:center}
  .empty code{font-family:var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-fg,#e6e6e6)}
  /* dependency graph */
  .gwrap{overflow:auto;border:1px solid var(--pl-color-border,#272b33);border-radius:var(--pl-radius-md,8px);
    background:var(--pl-color-bg-subtle,#15181d);padding:8px}
  .legend{display:flex;gap:14px;font-size:11px;color:var(--pl-color-fg-muted,#8a8f98);margin:8px 2px}
  .legend i{display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:4px}
  .plan{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
  .plan h3{font-size:12px;margin:0 0 6px;color:var(--pl-color-fg-muted,#8a8f98);text-transform:uppercase;letter-spacing:.04em}
  .row{font-size:12px;padding:4px 0;border-bottom:1px solid var(--pl-color-border,#272b33)}
  .mono{font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11px}
  /* drawer */
  .scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none}
  .scrim.on{display:block}
  .drawer{position:fixed;top:0;right:0;height:100%;width:min(440px,92vw);background:var(--pl-color-bg,#0b0d10);
    border-left:1px solid var(--pl-color-border,#272b33);transform:translateX(100%);transition:transform .15s;
    overflow:auto;padding:var(--pl-space-4,16px) var(--pl-space-5,20px)}
  .drawer.on{transform:translateX(0)}
  .dhead{display:flex;align-items:center;gap:8px;margin-bottom:4px}
  .dhead h2{font-size:16px;margin:0;font-weight:600;flex:1}
  .x{cursor:pointer;color:var(--pl-color-fg-muted,#8a8f98);font-size:18px;line-height:1}
  .feat{display:flex;align-items:baseline;gap:8px;padding:7px 0;border-bottom:1px solid var(--pl-color-border,#272b33)}
  .st{font-size:9px;text-transform:uppercase;letter-spacing:.03em;padding:1px 6px;border-radius:999px;flex:0 0 auto;
    border:1px solid var(--pl-color-border,#272b33);color:var(--pl-color-fg-muted,#8a8f98)}
  .st.done{color:var(--pl-color-success,#3fb950);border-color:var(--pl-color-success,#3fb950)}
  .st.in_progress,.st.in_review{color:var(--pl-color-accent,#6cb6ff);border-color:var(--pl-color-accent,#6cb6ff)}
  .st.blocked{color:var(--pl-color-danger,#f85149);border-color:var(--pl-color-danger,#f85149)}
  .ft{flex:1} .ft .t{font-size:12px} .ft .i{font-size:10px;opacity:.6}
  a{color:var(--pl-color-accent,#6cb6ff)}
</style>
<script>
  // Slug-aware base (so assets + fetches stay same-origin through the fleet proxy).
  var BASE = location.pathname.split("/plugins/")[0];
  (function(){ var l=document.createElement("link"); l.rel="stylesheet";
    l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
</head><body>
<div class="wrap">
  <header><h1>Portfolio</h1><div class="sub" id="summary"></div></header>
  <div class="tabs">
    <div class="tab on" data-tab="boards">Boards</div>
    <div class="tab" data-tab="deps">Dependencies</div>
  </div>
  <div id="err" class="err" hidden></div>
  <div id="boards" class="grid"></div>
  <div id="deps" hidden></div>
</div>
<div class="scrim" id="scrim"></div>
<div class="drawer" id="drawer"></div>
<script type="module">
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }
const api = (p) => kit.apiFetch(p).then(r => r.json());

const LANES = [["backlog","backlog"],["ready","ready"],["in_progress","prog"],["in_review","review"],["done","done"]];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ── Boards tab ──────────────────────────────────────────────────────────────────
function card(b){
  const reach = b.reachable, counts = b.counts || {};
  const lanes = LANES.map(([k,cls]) =>
    `<div class="lane ${cls}"><div class="n">${counts[k]||0}</div><div class="l">${k.replace("_"," ")}</div></div>`).join("");
  const blocked = (b.blocked||[]).length;
  const badge = b.spawned ? `<span class="badge eph">ephemeral</span>` : `<span class="badge">standing</span>`;
  const drained = b.drained ? `<span class="pill drn">drained</span>`
    : reach ? `<span class="pill">${(b.total||0)-(counts.done||0)} active</span>` : "";
  const blk = blocked ? `<span class="pill blk">${blocked} blocked</span> ` : "";
  const body = reach
    ? `<div class="lanes">${lanes}</div><div class="foot"><span>${blk}${drained}</span><span class="a2a">${esc(b.a2a||"")}</span></div>`
    : `<div class="err">${esc(b.error||"unreachable")}</div>`;
  return `<div class="card ${reach?"":"dim"}" ${reach?`data-board="${esc(b.board)}"`:""}>
    <div class="chead"><span class="dot ${reach?"up":"down"}"></span><span class="name">${esc(b.board)}</span><span class="grow"></span>${badge}</div>
    ${b.repo ? `<div class="repo">${esc(b.repo)}</div>` : ""}${body}</div>`;
}

// ── Dependencies tab — layered SVG graph (blocker → dependent) ─────────────────────
const SC = {satisfied:"var(--pl-color-success,#3fb950)",blocking:"var(--pl-color-warning,#d29922)",
  unknown:"var(--pl-color-fg-muted,#8a8f98)",dangling:"var(--pl-color-danger,#f85149)"};
function depGraph(plan){
  const links = plan.links || [];
  if (!links.length) return `<div class="empty">No cross-board dependencies. Use <code>portfolio_link</code> to record one.</div>`;
  const K = (b,f) => b+"␟"+f;
  const nodes = new Map();
  const add = (b,f) => { const k=K(b,f); if(!nodes.has(k)) nodes.set(k,{board:b,feature:f,key:k,layer:0}); return k; };
  const edges = links.map(l => ({from:add(l.from_board,l.from_feature), to:add(l.to_board,l.to_feature), status:l.status}));
  // "enables" DAG: to -> from; longest-path layering (cycles can't exist — rejected at link time).
  const adj=new Map(), ind=new Map();
  nodes.forEach((_,k)=>{adj.set(k,[]);ind.set(k,0);});
  edges.forEach(e=>{adj.get(e.to).push(e.from);ind.set(e.from,ind.get(e.from)+1);});
  const layer=new Map([...nodes.keys()].map(k=>[k,0])), id=new Map(ind);
  let q=[...nodes.keys()].filter(k=>id.get(k)===0);
  while(q.length){const k=q.shift();for(const nx of adj.get(k)){layer.set(nx,Math.max(layer.get(nx),layer.get(k)+1));id.set(nx,id.get(nx)-1);if(id.get(nx)===0)q.push(nx);}}
  const cols={}; nodes.forEach(n=>{n.layer=layer.get(n.key);(cols[n.layer]=cols[n.layer]||[]).push(n);});
  const NW=158,NH=34,COLW=200,RH=50,PX=14,PY=14;
  let maxL=0,maxR=0;
  Object.keys(cols).map(Number).forEach(c=>{maxL=Math.max(maxL,c);maxR=Math.max(maxR,cols[c].length);cols[c].forEach((n,i)=>{n.x=PX+c*COLW;n.y=PY+i*RH;});});
  const W=PX*2+maxL*COLW+NW, H=PY*2+maxR*RH;
  const pos=new Map([...nodes.values()].map(n=>[n.key,n]));
  const paths=edges.map(e=>{const a=pos.get(e.to),b=pos.get(e.from);
    const x1=a.x+NW,y1=a.y+NH/2,x2=b.x-2,y2=b.y+NH/2,mx=(x1+x2)/2;
    return `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none" stroke="${SC[e.status]||SC.unknown}" stroke-width="1.6" marker-end="url(#a)"/>`;}).join("");
  const rects=[...nodes.values()].map(n=>`<g><rect x="${n.x}" y="${n.y}" width="${NW}" height="${NH}" rx="6"
      fill="var(--pl-color-bg,#0b0d10)" stroke="var(--pl-color-border,#272b33)"/>
      <text x="${n.x+9}" y="${n.y+13}" font-size="9" fill="var(--pl-color-fg-muted,#8a8f98)">${esc(n.board)}</text>
      <text x="${n.x+9}" y="${n.y+26}" font-size="11" fill="var(--pl-color-fg,#e6e6e6)">${esc(n.feature)}</text></g>`).join("");
  return `<div class="legend"><span><i style="background:${SC.satisfied}"></i>satisfied</span>
      <span><i style="background:${SC.blocking}"></i>blocking</span>
      <span><i style="background:${SC.unknown}"></i>unknown</span>
      <span><i style="background:${SC.dangling}"></i>dangling</span><span>· arrow = blocker → dependent</span></div>
    <div class="gwrap"><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <defs><marker id="a" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 Z" fill="var(--pl-color-fg-muted,#8a8f98)"/></marker></defs>
      ${paths}${rects}</svg></div>`;
}
function planLists(plan){
  const r = (plan.ready_to_dispatch||[]).map(x=>`<div class="row"><span class="mono">${esc(x.board)}·${esc(x.feature)}</span></div>`).join("") || `<div class="sub">none</div>`;
  const b = (plan.blocked||[]).map(x=>`<div class="row"><span class="mono">${esc(x.board)}·${esc(x.feature)}</span>
      <div class="sub">waiting on ${x.blockers.map(k=>`${esc(k.board)}·${esc(k.feature)} (${esc(k.status)})`).join(", ")}</div></div>`).join("") || `<div class="sub">none</div>`;
  return `<div class="plan"><div><h3>Ready to dispatch</h3>${r}</div><div><h3>Blocked</h3>${b}</div></div>`;
}

// ── board drill-down drawer ───────────────────────────────────────────────────────
async function openBoard(name){
  const dr = document.getElementById("drawer");
  dr.innerHTML = `<div class="dhead"><h2>${esc(name)}</h2><span class="x" id="x">✕</span></div><div class="sub">loading…</div>`;
  document.getElementById("scrim").classList.add("on"); dr.classList.add("on");
  dr.querySelector("#x").onclick = closeDrawer;
  try {
    const d = await api("/api/plugins/portfolio/board/" + encodeURIComponent(name));
    const feats = d.features || [];
    const body = feats.length ? feats.map(f=>{
      const st = f.board_state || "backlog";
      const blk = (f.blocked||f.dag_blocked) ? `<span class="st blocked">blocked</span>` : "";
      const pr = f.pr_url ? ` · <a href="${esc(f.pr_url)}" target="_blank">PR</a>` : "";
      return `<div class="feat"><span class="st ${esc(st)}">${esc(st.replace("_"," "))}</span>
        <div class="ft"><div class="t">${esc(f.title||"(untitled)")}</div>
        <div class="i mono">${esc(f.id||"")}${pr}</div></div>${blk}</div>`;
    }).join("") : `<div class="empty">Board is empty.</div>`;
    dr.innerHTML = `<div class="dhead"><h2>${esc(name)}</h2><span class="x" id="x">✕</span></div>
      <div class="sub" style="margin-bottom:8px">${feats.length} feature${feats.length===1?"":"s"}</div>${body}`;
    dr.querySelector("#x").onclick = closeDrawer;
  } catch(e){ dr.innerHTML = `<div class="dhead"><h2>${esc(name)}</h2><span class="x" id="x" onclick="">✕</span></div><div class="err">${esc(""+e)}</div>`;
    dr.querySelector("#x").onclick = closeDrawer; }
}
function closeDrawer(){ document.getElementById("scrim").classList.remove("on"); document.getElementById("drawer").classList.remove("on"); }
document.getElementById("scrim").onclick = closeDrawer;
document.getElementById("boards").addEventListener("click", e=>{
  const c = e.target.closest("[data-board]"); if (c) openBoard(c.getAttribute("data-board"));
});

// ── tabs + load loop ──────────────────────────────────────────────────────────────
let tab = "boards";
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  tab = t.dataset.tab;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on", x.dataset.tab===tab));
  document.getElementById("boards").hidden = tab!=="boards";
  document.getElementById("deps").hidden = tab!=="deps";
  load();
});

async function load(){
  try {
    document.getElementById("err").hidden = true;
    if (tab === "boards"){
      const boards = (await api("/api/plugins/portfolio/overview")).boards || [];
      if (!boards.length){
        document.getElementById("boards").innerHTML = `<div class="empty">No teams yet. Ask the portfolio manager to <code>spinup_team(name, repo)</code>.</div>`;
        document.getElementById("summary").textContent = ""; return;
      }
      const active = boards.reduce((a,b)=>a+((b.total||0)-((b.counts||{}).done||0)),0);
      const blocked = boards.reduce((a,b)=>a+(b.blocked||[]).length,0);
      document.getElementById("summary").innerHTML = `<b>${boards.length}</b> board${boards.length>1?"s":""} · <b>${active}</b> active · <b>${blocked}</b> blocked`;
      document.getElementById("boards").innerHTML = boards.map(card).join("");
    } else {
      const plan = await api("/api/plugins/portfolio/plan");
      document.getElementById("deps").innerHTML = depGraph(plan) + planLists(plan);
    }
  } catch(e){ document.getElementById("err").hidden = false; document.getElementById("err").textContent = "Could not load: " + e; }
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
    """The GATED data router (mounted at /api/plugins/portfolio). Reflects the same data
    the portfolio tools compute — overview (rollup), per-board features, and the plan."""
    import asyncio

    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/overview")
    async def _overview() -> dict:
        from . import _BoardUnavailable, _fetch_board_features, _load_teams, _remote_by_name, _rollup_one
        from graph.fleet import supervisor

        teams = {t["name"]: t for t in _load_teams()}
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

    @router.get("/board/{name}")
    async def _board(name: str) -> dict:
        """The full feature list for ONE board — the card drill-down."""
        from . import _BoardUnavailable, _fetch_board_features, _remote_by_name

        rec = _remote_by_name(name)
        if rec is None:
            return {"board": name, "features": [], "error": "not resolvable"}
        try:
            feats = await _fetch_board_features(rec)
        except _BoardUnavailable as exc:
            return {"board": name, "features": [], "error": str(exc)}
        return {"board": name, "features": feats}

    @router.get("/plan")
    async def _plan() -> dict:
        """The cross-board dependency graph (same as the portfolio_plan tool)."""
        from . import _compute_plan

        return await _compute_plan()

    return router
