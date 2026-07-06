"""Project registry tests — the clawpatch structural-review gate.

Clawpatch reads ``.proto/project.yaml`` to decide whether a repo is part of a
known project and therefore eligible for structural (not just diff-only)
reviews. These tests assert the file exists, has the required shape, and that
the registry id matches the plugin id so a future rename can't silently drop
the repo from clawpatch's index.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _registry():
    return yaml.safe_load((ROOT / ".proto" / "project.yaml").read_text())


def test_project_registry_exists():
    """The file must exist — without it, clawpatch falls back to diff-only reviews."""
    assert (ROOT / ".proto" / "project.yaml").exists()


def test_project_registry_has_required_fields():
    r = _registry()
    for key in ("id", "name", "type", "description", "enabled"):
        assert key in r, f"missing required field: {key}"


def test_project_registry_id_matches_plugin_id():
    """The registry id and the plugin id must agree — a drift means clawpatch
    can't find this repo in the index even though the file exists."""
    r = _registry()
    plugin = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert r["id"] == plugin["id"] + "-plugin", (
        f"registry id '{r['id']}' doesn't match plugin id '{plugin['id']}' + '-plugin'"
    )


def test_project_registry_type_is_plugin():
    r = _registry()
    assert r["type"] == "plugin"


def test_project_registry_enabled():
    """Structural reviews are gated on ``enabled: true`` — make sure they're on."""
    r = _registry()
    assert r["enabled"] is True


def test_project_registry_description_is_nonempty():
    r = _registry()
    assert r["description"] and len(r["description"].strip()) > 10
