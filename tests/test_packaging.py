"""Packaging + contract tests — the cheap guards against "ships but subtly broken":
a renamed manifest field, or the pyproject/manifest/tag versions drifting apart.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _manifest():
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())


def test_manifest_has_the_required_shape():
    m = _manifest()
    assert m["id"] == "portfolio"
    assert m["name"] and m["version"]
    assert m["enabled"] is False  # ships DISABLED — enabling on the PM is the trust decision


def test_manifest_and_pyproject_versions_agree():
    m = _manifest()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert m["version"] == pyproject["project"]["version"], (
        "manifest and pyproject versions drifted — keep them in lockstep (and the release tag too)"
    )
