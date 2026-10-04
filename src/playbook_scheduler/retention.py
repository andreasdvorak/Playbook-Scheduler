"""Remove run records that are older than the retention period."""

from datetime import datetime, timedelta, timezone
from pathlib import Path


def remove_expired_runs(
    runs_directory: Path, retention_days: int, now: datetime | None = None
) -> int:
    """Delete run records whose modification time is before the cutoff.

    Args:
        runs_directory: Directory with the JSON run records.
        retention_days: Number of days records are kept.
        now: Reference time; defaults to the current time. Used by tests.

    Returns:
        Number of deleted records.

    Raises:
        ValueError: If ``retention_days`` is less than 1.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be a positive integer.")
    if not runs_directory.exists():
        return 0

    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time.timestamp() - timedelta(days=retention_days).total_seconds()
    removed = 0
    for run_file in runs_directory.glob("*.json"):
        if run_file.stat().st_mtime < cutoff:
            run_file.unlink()
            removed += 1
    return removed
