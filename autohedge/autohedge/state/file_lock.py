"""Small fcntl-based file lock helpers for local JSONL coordination."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator, TextIO


LOCK_SH = fcntl.LOCK_SH
LOCK_EX = fcntl.LOCK_EX


@contextmanager
def locked_open(
    path: str | Path,
    mode: str,
    *,
    lock: int,
    encoding: str = "utf-8",
) -> Iterator[TextIO]:
    """Open a text file and hold an advisory flock for the context duration."""
    file_path = Path(path)
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open(mode, encoding=encoding) as handle:
        fcntl.flock(handle.fileno(), lock)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

