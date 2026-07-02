"""_store_path resolves PM data files on BOTH host generations.

protoAgent 0.77 (#1481) deleted the legacy ``scope_leaf`` knob — a plugin importing
it raises ImportError on every current host (the bug behind the broken portfolio
view). These pin the contract: prefer ``instance_paths().store()``, fall back to
``scope_leaf(data_home() / name)`` only on pre-0.77 hosts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import portfolio


def test_prefers_instance_paths_store(monkeypatch):
    # Poison the legacy path: if the fallback ran, the result would be /nope/x.json.
    paths = sys.modules["infra.paths"]
    monkeypatch.setattr(paths, "scope_leaf", lambda p: Path("/nope") / Path(p).name)
    assert portfolio._store_path("x.json") == Path("/tmp/x.json")


def test_falls_back_to_scope_leaf_on_pre_077_hosts(monkeypatch):
    # A pre-#1481 host has no instance_paths at all — only the legacy pair.
    paths = sys.modules["infra.paths"]
    monkeypatch.delattr(paths, "instance_paths")
    assert portfolio._store_path("x.json") == Path("/tmp/x.json")


def test_all_three_stores_resolve_per_instance():
    assert portfolio._snapshot_path() == Path("/tmp/portfolio_snapshot.json")
    assert portfolio._links_path() == Path("/tmp/portfolio_links.json")
    assert portfolio._teams_path() == Path("/tmp/portfolio_teams.json")
