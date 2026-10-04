"""Provide file helpers for threads and processes sharing the same directories."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if os.name == "nt":
    import msvcrt  # pylint: disable=import-error
else:
    import fcntl

LOCK_DIRECTORY_NAME = ".locks"
# Job names must start with a letter or number, so this name cannot collide.
REPORT_LOCK_NAME = "_report"
LOCK_POLL_SECONDS = 0.1


class LockBusy(RuntimeError):
    """Raised when a non-blocking lock is held by another thread or process."""


def lock_path(runs_directory: Path, name: str) -> Path:
    """Return the lock file path for ``name`` below ``runs_directory``.

    Args:
        runs_directory: Directory with the run records.
        name: Job name or ``REPORT_LOCK_NAME``.

    Returns:
        Path of the lock file in the ``.locks`` subdirectory.
    """
    return runs_directory / LOCK_DIRECTORY_NAME / f"{name}.lock"


def _try_lock(handle: IO[bytes]) -> bool:
    """Try to take an exclusive lock on ``handle`` without waiting.

    Args:
        handle: Open handle of the lock file.

    Returns:
        ``True`` if the lock was taken, ``False`` if it is held elsewhere.
    """
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    """Release the lock taken by ``_try_lock``.

    Args:
        handle: Open handle of the lock file.
    """
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the block.

    Every call opens its own file handle, so the lock also excludes other
    threads of the same process. Lock files are never deleted: removing one
    while another process waits on it would let two holders in at once.

    Args:
        path: Lock file; it and its parent directory are created if missing.
        blocking: Wait until the lock is free instead of failing immediately.

    Yields:
        Nothing; the lock is held while the ``with`` block runs.

    Raises:
        LockBusy: If ``blocking`` is false and the lock is held elsewhere.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        while not _try_lock(handle):
            if not blocking:
                raise LockBusy(f"Lock is held by another run: {path}")
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(handle)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` so that readers see either the old or the new file.

    The text is written to a temporary file in the same directory, which then
    replaces ``path`` in a single ``os.replace`` call.

    Args:
        path: Target file; its parent directory is created if missing.
        text: Content to write, encoded as UTF-8.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
