"""Tests for reloading scheduler configuration and rescheduling jobs."""

import os
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from playbook_scheduler.config import load_config
from playbook_scheduler.scheduler import ConfigWatcher


def write_config(directory: Path, jobs: dict[str, str]) -> Path:
    """Write a config file with the requested job names and cron schedules."""
    (directory / "playbook.yml").write_text("---\n", encoding="utf-8")
    (directory / "hosts.ini").write_text("localhost\n", encoding="utf-8")
    entries = "".join(
        f"""
  - name: {name}
    cron: "{cron}"
    playbook: playbook.yml
    inventory: hosts.ini
"""
        for name, cron in jobs.items()
    )
    config_path = directory / "config.yaml"
    config_path.write_text(f"timezone: UTC\njobs:{entries}", encoding="utf-8")
    return config_path


def touch_later(path: Path) -> None:
    """Advance a config file's modification time for watcher tests."""
    # Guarantee a new mtime even on file systems with coarse timestamps.
    status = path.stat()
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))


@pytest.fixture(name="background_scheduler")
def _background_scheduler():
    """Provide a paused scheduler for deterministic watcher tests."""
    instance = BackgroundScheduler(timezone="UTC")
    instance.start(paused=True)
    yield instance
    instance.shutdown(wait=False)


def test_watcher_reschedules_changed_jobs(tmp_path: Path, background_scheduler) -> None:
    """Apply additions, removals, and schedule changes after a config edit."""
    config_path = write_config(tmp_path, {"keep": "0 2 * * *", "drop": "0 3 * * *"})
    watcher = ConfigWatcher(background_scheduler, load_config(config_path))
    watcher.schedule_jobs()

    assert watcher.check() is False

    write_config(tmp_path, {"keep": "30 4 * * *", "new": "0 5 * * *"})
    touch_later(config_path)

    assert watcher.check() is True
    assert {job.id for job in background_scheduler.get_jobs()} == {"keep", "new"}
    assert "hour='4'" in str(background_scheduler.get_job("keep").trigger)
    assert background_scheduler.get_job("keep").args[1] is watcher.config
    assert (tmp_path / "reports" / "index.html").exists()


def test_watcher_keeps_previous_config_when_reload_fails(
    tmp_path: Path, background_scheduler, caplog
) -> None:
    """Keep the active schedule when an edited config is invalid."""
    config_path = write_config(tmp_path, {"keep": "0 2 * * *"})
    watcher = ConfigWatcher(background_scheduler, load_config(config_path))
    watcher.schedule_jobs()
    previous = watcher.config

    write_config(tmp_path, {"keep": "not a cron"})
    touch_later(config_path)

    assert watcher.check() is False
    assert watcher.config is previous
    assert [job.id for job in background_scheduler.get_jobs()] == ["keep"]
    assert "keeping previous configuration" in caplog.text
    # The broken file is not reloaded again until it changes once more.
    assert watcher.check() is False
