"""Grounding-doc tests — PROTO.md + the CLAUDE.md / AGENTS.md pointer files.

These pin the contract: every coding agent that lands in this repo finds the same
instructions, regardless of which agent harness (Claude Code, Aider, etc.) is driving.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _proto():
    return (ROOT / "PROTO.md").read_text()


def _pointer(name: str) -> str:
    return (ROOT / name).read_text()


# ── existence ──────────────────────────────────────────────────────────────────


def test_proto_md_exists_at_repo_root():
    assert (ROOT / "PROTO.md").is_file()


def test_claude_md_exists_at_repo_root():
    assert (ROOT / "CLAUDE.md").is_file()


def test_agents_md_exists_at_repo_root():
    assert (ROOT / "AGENTS.md").is_file()


# ── pointer files contain only a reference to PROTO.md ─────────────────────────


def test_claude_md_points_to_proto():
    text = _pointer("CLAUDE.md")
    assert "PROTO.md" in text, "CLAUDE.md must reference PROTO.md"


def test_agents_md_points_to_proto():
    text = _pointer("AGENTS.md")
    assert "PROTO.md" in text, "AGENTS.md must reference PROTO.md"


def test_pointer_files_are_thin():
    """CLAUDE.md and AGENTS.md should be thin pointer files — not re-implementation of PROTO.md."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = _pointer(name)
        # Each pointer file should be short (a few lines max) — if someone pastes PROTO.md
        # content into it, that's a maintenance hazard.
        assert len(text.strip().splitlines()) <= 5, f"{name} should be a thin pointer, not a copy of PROTO.md"


# ── PROTO.md covers the required sections ─────────────────────────────────────


def test_proto_covers_stack():
    text = _proto()
    # Python 3.11+ and pure composition (no runtime pip deps) are the defining traits
    assert "Python 3.11" in text or "py311" in text
    assert "pure composition" in text.lower() or "no runtime" in text.lower()
    assert "langchain-core" in text or "langchain_core" in text
    assert "FastAPI" in text


def test_proto_covers_build_run_test_command():
    text = _proto()
    assert "ruff check" in text
    assert "ruff format" in text
    assert "pytest" in text
    assert "requirements-dev.txt" in text


def test_proto_covers_conventions():
    text = _proto()
    assert "120" in text  # line-length
    assert "asyncio_mode" in text
    assert "lockstep" in text.lower() or "lock step" in text.lower() or "lockstep" in text.lower()


def test_proto_covers_shared_deps_location():
    text = _proto()
    assert "conftest.py" in text
    assert "stubs" in text.lower() or "stub" in text.lower()


def test_proto_covers_dos_donts():
    text = _proto()
    # Must mention both do and don't, and the specific .proto/ scratch rule
    assert "don't" in text.lower() or "donot" in text.lower() or "don't" in text.lower()
    assert ".proto/memory" in text or ".proto/" in text


def test_proto_mentions_packaging_guard():
    """The version-lockstep do is guarded by test_packaging.py — PROTO.md should reference that."""
    text = _proto()
    assert "test_packaging" in text
