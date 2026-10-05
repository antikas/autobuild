from __future__ import annotations

from pathlib import Path

import pytest
from scanner_retry import run_retrying


def _step(root: Path, *argv: str) -> str:
    completed = run_retrying(argv, cwd=root)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        pytest.fail(f"pinax probe step {' '.join(argv[:4])} failed with exit {completed.returncode}: {detail}")
    return completed.stdout.strip()


@pytest.fixture(scope="session")
def self_committing_pinax(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip a real-Pinax test when the installed Pinax completes a mutation
    and leaves it uncommitted: AutoBuild refuses such a tracker, so the test
    could only observe that refusal, and the stub-Pinax tests cover the adapter
    either way. A probe step that still fails after the scanner retry is a
    genuine error and fails the test."""

    root = tmp_path_factory.mktemp("pinax-probe")
    _step(root, "git", "init", "-b", "main", str(root))
    _step(root, "git", "-C", str(root), "config", "user.name", "AutoBuild Probe")
    _step(root, "git", "-C", str(root), "config", "user.email", "autobuild@example.invalid")
    _step(root, "pinax", "--root", str(root), "init", "--actor", "probe@autobuild")
    _step(root, "git", "-C", str(root), "add", ".")
    _step(root, "git", "-C", str(root), "commit", "-m", "initial")
    head = _step(root, "git", "-C", str(root), "rev-parse", "HEAD")
    _step(
        root,
        "pinax", "--root", str(root), "add", "--title", "Probe", "--prefix", "prb",
        "--actor", "probe@autobuild", "--json",
    )
    leftover = _step(
        root, "git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", ".ergon"
    )
    moved = _step(root, "git", "-C", str(root), "rev-parse", "HEAD") != head
    if leftover and not moved:
        pytest.skip(
            "the installed pinax leaves its mutations uncommitted; "
            "these tests need pinax-tracker 0.2.1 or later"
        )
