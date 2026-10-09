"""Tests for file locking, scheduled runs, and runtime configuration behavior."""

import json
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

from playbook_scheduler import cli, files, scheduler, service
from playbook_scheduler.config import AppConfig, ConfigError, Job, load_config
from playbook_scheduler.files import (
    REPORT_LOCK_NAME,
    LockBusy,
    atomic_write_text,
    file_lock,
    lock_path,
)
from playbook_scheduler.service import JobAlreadyRunning, execute_job


def make_config(tmp_path: Path) -> AppConfig:
    """Build an application config with a single local job."""
    job = Job(
        name="local_ping",
        cron="0 2 * * *",
        playbook=tmp_path / "ping.yml",
        inventory=tmp_path / "hosts.ini",
        working_directory=tmp_path,
        timeout_seconds=30,
        extra_args=(),
    )
    return AppConfig(
        config_path=tmp_path / "config.yaml",
        timezone="UTC",
        runs_directory=tmp_path / "runs",
        reports_directory=tmp_path / "reports",
        retention_days=30,
        jobs=(job,),
    )


def test_non_blocking_lock_is_busy_while_held(tmp_path: Path) -> None:
    """Reject non-blocking lock acquisition while another holder owns it."""
    path = tmp_path / "locks" / "job.lock"
    with file_lock(path), pytest.raises(LockBusy), file_lock(path, blocking=False):
        pass
    # Released after the block, and the lock file stays in place.
    with file_lock(path, blocking=False):
        pass
    assert path.exists()


def test_blocking_lock_waits_for_other_thread(tmp_path: Path) -> None:
    """Wait for a lock held by another thread, then acquire it in order."""
    path = tmp_path / "report.lock"
    order: list[str] = []
    held = threading.Event()

    def holder() -> None:
        """Hold the lock briefly so the main thread has to wait."""
        with file_lock(path):
            held.set()
            time.sleep(0.3)
            order.append("holder")

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait()
    with file_lock(path):
        order.append("waiter")
    thread.join()

    assert order == ["holder", "waiter"]


def test_atomic_write_replaces_file_without_leftovers(tmp_path: Path) -> None:
    """Replace a file atomically and leave no temporary files behind."""
    target = tmp_path / "reports" / "index.html"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert [path.name for path in target.parent.iterdir()] == ["index.html"]


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX file permissions")
def test_atomic_write_applies_umask_to_file_permissions(
    tmp_path: Path, monkeypatch
) -> None:
    """Make atomic output readable by the group allowed by the umask."""
    monkeypatch.setattr(files, "_UMASK", 0o027)
    target = tmp_path / "reports" / "index.html"

    files.atomic_write_text(target, "report")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_execute_job_skips_job_that_is_already_running(
    tmp_path: Path, monkeypatch
) -> None:
    """Avoid starting duplicate work and record the skipped run and report."""
    config = make_config(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(service, "run_job", lambda job, _: started.append(job.name))

    job_lock = lock_path(config.runs_directory, "local_ping")
    with file_lock(job_lock), pytest.raises(JobAlreadyRunning):
        execute_job(config.jobs[0], config)

    assert not started
    skipped = json.loads(next(config.runs_directory.glob("*.json")).read_text())
    assert skipped["status"] == "skipped"
    assert skipped["error"] == "Skipped: Job local_ping is already running."
    report = (config.reports_directory / "index.html").read_text(encoding="utf-8")
    assert 'class="badge skipped"' in report


def test_execute_job_runs_and_writes_report(tmp_path: Path, monkeypatch) -> None:
    """Write a report after successfully executing a job."""
    config = make_config(tmp_path)
    monkeypatch.setattr(service, "run_job", lambda job, _: {"job": job.name})

    assert execute_job(config.jobs[0], config) == {"job": "local_ping"}
    assert (config.reports_directory / "index.html").exists()
    assert lock_path(config.runs_directory, REPORT_LOCK_NAME).exists()


def test_run_command_reports_running_job(tmp_path: Path, monkeypatch, capsys) -> None:
    """Return the already-running exit code when the job lock is held."""
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config)

    with file_lock(lock_path(config.runs_directory, "local_ping")):
        exit_code = cli.main(["run", "local_ping"])

    assert exit_code == 3
    assert "Job local_ping is already running." in capsys.readouterr().err


def test_scheduled_run_of_running_job_is_skipped(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Log a scheduled run as skipped when its job is already running."""
    config = make_config(tmp_path)

    def busy(job: Job, _: AppConfig) -> None:
        """Simulate another run holding the job lock."""
        raise JobAlreadyRunning(f"Job {job.name} is already running.")

    monkeypatch.setattr(scheduler, "execute_job", busy)

    scheduler.run_scheduled_job(config.jobs[0], config)

    assert "Skipped scheduled run: Job local_ping is already running." in caplog.text


def test_scheduled_run_logs_start_and_outcome(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Log both the start and failed outcome of a scheduled run."""
    config = make_config(tmp_path)
    caplog.set_level("INFO")
    monkeypatch.setattr(
        scheduler,
        "execute_job",
        lambda job, _: {
            "status": "failed",
            "duration_seconds": 1.5,
            "run_id": "abc",
            "error": "Timed out after 30 seconds.",
        },
    )

    scheduler.run_scheduled_job(config.jobs[0], config)

    assert "Starting job local_ping" in caplog.text
    finished = caplog.records[-1]
    assert finished.levelname == "ERROR"
    assert finished.getMessage() == (
        "Job local_ping finished: failed after 1.5s (run abc): "
        "Timed out after 30 seconds."
    )


def test_configure_logging_adds_timestamps(capsys, monkeypatch) -> None:
    """Include timestamps on ordinary stderr logging and suppress scheduler noise."""
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    root.handlers = []
    try:
        scheduler.configure_logging()
        logging.getLogger("playbook_scheduler.scheduler").info("hello")
        logging.getLogger("apscheduler.scheduler").info("noise")
    finally:
        root.handlers, root.level = previous_handlers, previous_level
        logging.getLogger("apscheduler").setLevel(logging.NOTSET)

    err = capsys.readouterr().err
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\S* INFO hello$", err, re.MULTILINE)
    assert "noise" not in err


def test_configure_logging_uses_syslog_priorities_under_journal(
    capsys, monkeypatch
) -> None:
    """Prefix journal output lines with their syslog priorities."""
    monkeypatch.setattr(scheduler, "_stderr_is_journal", lambda: True)
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    root.handlers = []
    try:
        scheduler.configure_logging()
        log = logging.getLogger("playbook_scheduler.scheduler")
        log.info("hello")
        log.warning("careful")
        log.error("first\nsecond")
    finally:
        root.handlers, root.level = previous_handlers, previous_level
        logging.getLogger("apscheduler").setLevel(logging.NOTSET)

    assert capsys.readouterr().err.splitlines() == [
        "<6>INFO hello",
        "<4>WARNING careful",
        "<3>ERROR first",
        "<3>second",
    ]


def test_stderr_is_journal_compares_journal_stream(
    tmp_path, monkeypatch
) -> None:  # pylint: disable=protected-access
    """Recognize stderr only when its device and inode match JOURNAL_STREAM."""
    with (tmp_path / "stderr").open("w") as stream:
        status = os.fstat(stream.fileno())
        monkeypatch.setattr(scheduler.sys, "stderr", stream)

        monkeypatch.setenv("JOURNAL_STREAM", f"{status.st_dev}:{status.st_ino}")
        assert scheduler._stderr_is_journal()  # pylint: disable=protected-access

        monkeypatch.setenv("JOURNAL_STREAM", f"{status.st_dev}:{status.st_ino + 1}")
        assert not scheduler._stderr_is_journal()  # pylint: disable=protected-access

        monkeypatch.delenv("JOURNAL_STREAM")
        assert not scheduler._stderr_is_journal()  # pylint: disable=protected-access


def write_config(directory: Path, extra: str) -> Path:
    """Write a minimal job config prefixed by additional application options."""
    (directory / "playbook.yml").write_text("---\n", encoding="utf-8")
    (directory / "hosts.ini").write_text("localhost\n", encoding="utf-8")
    config_path = directory / "config.yaml"
    config_path.write_text(
        f"""{extra}
jobs:
  - name: ping
    cron: "0 2 * * *"
    playbook: playbook.yml
    inventory: hosts.ini
""",
        encoding="utf-8",
    )
    return config_path


def test_max_parallel_jobs_defaults_to_ten(tmp_path: Path) -> None:
    """Use the documented default maximum number of parallel jobs."""
    assert load_config(write_config(tmp_path, "")).max_parallel_jobs == 10


def test_report_output_lines_is_read_and_validated(tmp_path: Path) -> None:
    """Load the report output line limit and reject non-positive values."""
    assert load_config(write_config(tmp_path, "")).report_output_lines == 200
    config = load_config(write_config(tmp_path, "report_output_lines: 50"))
    assert config.report_output_lines == 50
    with pytest.raises(ConfigError, match="report_output_lines"):
        load_config(write_config(tmp_path, "report_output_lines: 0"))


def test_max_parallel_jobs_is_read(tmp_path: Path) -> None:
    """Load an explicitly configured parallel job limit."""
    config = load_config(write_config(tmp_path, "max_parallel_jobs: 2"))

    assert config.max_parallel_jobs == 2


@pytest.mark.parametrize("value", ["0", "two", "1.5"])
def test_invalid_max_parallel_jobs_is_reported(tmp_path: Path, value: str) -> None:
    """Reject invalid configured values for maximum parallel jobs."""
    with pytest.raises(ConfigError, match="max_parallel_jobs"):
        load_config(write_config(tmp_path, f"max_parallel_jobs: {value}"))
