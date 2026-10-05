"""The test tree's one retry for an on-access scanner's transient refusals.

A scanner can refuse a fresh file under `.git/objects` for a moment. A git
command is re-issued. A Pinax command whose commit the scanner refused (exit 7
with a `.git/objects` path in the refusal) has already appended its event, so
it is never re-issued, which would append the event twice: the shard and the
projection are committed the way Pinax's own message instructs. Any other
Pinax exit 7, such as a hook refusal, is returned unchanged.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

_TRANSIENT = ("permission denied", "unpacker error")
_ATTEMPTS = 5


def _once(argv: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _transient(completed: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{completed.stderr}\n{completed.stdout}".casefold()
    return completed.returncode != 0 and any(marker in detail for marker in _TRANSIENT)


def run_retrying(
    argv: Sequence[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv``, retrying only a scanner refusal; any other result is returned."""

    # The program, or the script an interpreter runs, is Pinax.
    is_pinax = any(Path(part).stem == "pinax" for part in argv[:2])
    for attempt in range(_ATTEMPTS):
        completed = _once(argv, cwd)
        if not _transient(completed):
            return completed
        output = f"{completed.stderr}\n{completed.stdout}".replace("\\", "/")
        refused_object = ".git/objects/" in output
        if is_pinax and completed.returncode == 7 and refused_object:
            root = argv[list(argv).index("--root") + 1]
            for git_args in (
                ("add", "-A", "--", ".ergon"),
                ("commit", "-m", "pinax: commit refused by the scanner, completed"),
            ):
                done = run_retrying(("git", "-C", root, *git_args))
                if done.returncode != 0:
                    return done
            return subprocess.CompletedProcess(list(argv), 0, completed.stdout, completed.stderr)
        if is_pinax or attempt == _ATTEMPTS - 1:
            return completed
        time.sleep(0.2 * (attempt + 1))
    return completed
