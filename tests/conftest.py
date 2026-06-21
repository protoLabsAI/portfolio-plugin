"""Test bootstrap — register this repo as the ``portfolio`` package and stub the host
seams the plugin imports lazily, so the suite runs standalone (no protoAgent in CI).

The plugin uses absolute imports to host packages — ``graph.fleet.supervisor``,
``plugins.delegates.adapters``, ``infra.paths``, ``security.policy`` — all INSIDE
function bodies, so importing the module is clean; but the tests patch them, which
needs them importable. We register lightweight stubs (mirroring projectBoard-plugin's
``graph.sdk`` stub). The real implementations are provided by protoAgent at runtime.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "portfolio"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _module
    _spec.loader.exec_module(_module)


def _pkg(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__path__ = []  # mark as a package so submodules resolve
        sys.modules[name] = mod
    return mod


# ── graph.fleet.supervisor — the fleet / team-agent registry ─────────────────────
if "graph.fleet.supervisor" not in sys.modules:
    _pkg("graph")
    _pkg("graph.fleet")

    async def _refresh_remote_probes():
        return None

    sup = types.ModuleType("graph.fleet.supervisor")
    sup.list_remotes = lambda: []  # tests patch
    sup.status = lambda: []  # tests patch
    sup.add_remote = lambda *a, **k: {}  # tests patch
    sup.remove_remote = lambda *a, **k: {}  # tests patch — team teardown
    sup.start = lambda *a, **k: {}  # tests patch — spawn a team agent
    sup.stop = lambda *a, **k: {}  # tests patch — stop a team agent
    sup.refresh_remote_probes = _refresh_remote_probes
    sys.modules["graph.fleet.supervisor"] = sup
    sys.modules["graph"].fleet = sys.modules["graph.fleet"]
    sys.modules["graph.fleet"].supervisor = sup


# ── graph.workspaces.manager — the workspace (scoped agent) lifecycle ────────────
if "graph.workspaces.manager" not in sys.modules:
    _pkg("graph")
    _pkg("graph.workspaces")

    mgr = types.ModuleType("graph.workspaces.manager")
    mgr.create = lambda *a, **k: {}  # tests patch — clone a team config into a workspace
    mgr.remove = lambda *a, **k: {}  # tests patch — purge a team workspace
    sys.modules["graph.workspaces.manager"] = mgr
    sys.modules["graph"].workspaces = sys.modules["graph.workspaces"]
    sys.modules["graph.workspaces"].manager = mgr


# ── graph.config_io — comment-preserving YAML doc load/save (real = ruamel) ──────
if "graph.config_io" not in sys.modules:
    _pkg("graph")

    import yaml as _yaml

    def _load_yaml_doc(path):
        return _yaml.safe_load(Path(path).read_text()) or {}

    def _save_yaml_doc(doc, path):  # real signature is save_yaml_doc(doc, path) — doc first
        Path(path).write_text(_yaml.safe_dump(doc, sort_keys=False))

    cio = types.ModuleType("graph.config_io")
    cio.load_yaml_doc = _load_yaml_doc
    cio.save_yaml_doc = _save_yaml_doc
    sys.modules["graph.config_io"] = cio
    sys.modules["graph"].config_io = cio


# ── graph.plugins.installer — where git-installed plugins live ───────────────────
if "graph.plugins.installer" not in sys.modules:
    _pkg("graph")
    _pkg("graph.plugins")

    inst = types.ModuleType("graph.plugins.installer")
    inst.live_plugins_dir = lambda: Path("/tmp/host-plugins")  # tests patch _host_plugins_dir
    sys.modules["graph.plugins.installer"] = inst
    sys.modules["graph"].plugins = sys.modules["graph.plugins"]
    sys.modules["graph.plugins"].installer = inst


# ── plugins.delegates.adapters — the A2A dispatch primitive ──────────────────────
if "plugins.delegates.adapters" not in sys.modules:
    _pkg("plugins")
    _pkg("plugins.delegates")

    @dataclass
    class Delegate:
        name: str = ""
        type: str = ""
        url: str = ""
        auth_scheme: str = ""
        auth_token: str = ""

    class _A2aAdapter:
        async def dispatch(self, d, query, *, timeout=None):
            raise RuntimeError("ADAPTERS['a2a'].dispatch must be monkeypatched in tests")

    adapters = types.ModuleType("plugins.delegates.adapters")
    adapters.ADAPTERS = {"a2a": _A2aAdapter()}
    adapters.Delegate = Delegate
    sys.modules["plugins.delegates.adapters"] = adapters
    sys.modules["plugins"].delegates = sys.modules["plugins.delegates"]
    sys.modules["plugins.delegates"].adapters = adapters


# ── infra.paths — scoped data dirs + atomic write ────────────────────────────────
if "infra.paths" not in sys.modules:
    _pkg("infra")

    def _atomic_write(path, text, *, mode=None):
        Path(path).write_text(text)

    paths = types.ModuleType("infra.paths")
    paths.data_home = lambda: Path("/tmp")
    paths.scope_leaf = lambda p: Path(p)
    paths.atomic_write = _atomic_write
    sys.modules["infra.paths"] = paths
    sys.modules["infra"].paths = paths


# ── security.policy — the egress / SSRF guard ────────────────────────────────────
if "security.policy" not in sys.modules:
    _pkg("security")
    policy = types.ModuleType("security.policy")
    policy.check_url = lambda url: ""  # "" = allowed; tests patch as needed
    sys.modules["security.policy"] = policy
    sys.modules["security"].policy = policy
