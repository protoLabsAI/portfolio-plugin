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
    sup.refresh_remote_probes = _refresh_remote_probes
    sys.modules["graph.fleet.supervisor"] = sup
    sys.modules["graph"].fleet = sys.modules["graph.fleet"]
    sys.modules["graph.fleet"].supervisor = sup


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
