import os
import signal
import threading
import time
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from playbook_scheduler import runner, scheduler
from playbook_scheduler.config import Job
from playbook_scheduler.runner import run_job, stop_running_jobs
from playbook_scheduler.scheduler import StopSignalHandler

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell scripts")


@pytest.fixture(autouse=True)
def reset_stop_request():
    yield
    runner._stop_requested.clear()


def make_job(tmp_path: Path, script: str) -> Job:
    """Return a job whose ``ansible-playbook`` is the given shell script."""
    virtualenv = tmp_path / ".venv"
    executable = virtualenv / "bin" / "ansible-playbook"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    executable.chmod(0o755)
    return Job(
        name="slow",
        cron="0 2 * * *",
        playbook=tmp_path / "playbook.yml",
        inventory=tmp_path / "hosts.ini",
        working_directory=tmp_path,
        timeout_seconds=60,
        extra_args=(),
        ansible_venv=virtualenv,
    )


def start_in_thread(job: Job, runs_directory: Path) -> tuple[threading.Thread, dict]:
    """Run ``job`` in a thread and wait until its process has started."""
    results: dict = {}
    thread = threading.Thread(
        target=lambda: results.update(run_job(job, runs_directory))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not runner._running_processes and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)  # Let the script print its first line.
    return thread, results


def test_stop_running_jobs_records_interrupted_run(tmp_path: Path) -> None:
    job = make_job(tmp_path, "echo started\nexec sleep 30")
    thread, result = start_in_thread(job, tmp_path / "runs")

    stop_running_jobs()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result["status"] == "failed"
    assert result["error"] == "Interrupted by shutdown."
    assert result["return_code"] == -signal.SIGTERM
    assert result["stdout"] == "started\n"
    assert list((tmp_path / "runs").glob("*.json"))


def test_job_started_after_stop_request_is_interrupted(tmp_path: Path) -> None:
    job = make_job(tmp_path, "exec sleep 30")
    stop_running_jobs()

    started = time.monotonic()
    result = run_job(job, tmp_path / "runs")

    assert time.monotonic() - started < 10
    assert result["error"] == "Interrupted by shutdown."


def test_forced_stop_kills_process_that_ignores_terminate(tmp_path: Path) -> None:
    # An ignored signal stays ignored across exec, so sleep ignores SIGTERM.
    job = make_job(tmp_path, "trap '' TERM\nexec sleep 30")
    thread, result = start_in_thread(job, tmp_path / "runs")

    stop_running_jobs()
    thread.join(timeout=0.5)
    assert thread.is_alive()

    stop_running_jobs(force=True)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result["return_code"] == -signal.SIGKILL
    assert result["error"] == "Interrupted by shutdown."


def test_signal_handler_stops_scheduler_then_forces(monkeypatch, caplog) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        scheduler, "stop_running_jobs", lambda force=False: calls.append(force)
    )
    caplog.set_level("INFO")
    background = BackgroundScheduler(timezone="UTC")
    background.start(paused=True)
    handler = StopSignalHandler(background)

    handler(signal.SIGTERM, None)

    assert calls == [False]
    assert not background.running
    assert "Received SIGTERM; stopping running jobs." in caplog.text

    handler(signal.SIGINT, None)

    assert calls == [False, True]
    assert "Received SIGINT again" in caplog.text
