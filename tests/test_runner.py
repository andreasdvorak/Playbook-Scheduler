"""Tests for Ansible process execution and run-record persistence."""

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from playbook_scheduler.config import Job
from playbook_scheduler.runner import STOP_GRACE_SECONDS, run_job


def make_job(tmp_path: Path) -> Job:
    """Build a representative job for runner tests."""
    return Job(
        name="patch_linux",
        cron="0 2 * * *",
        playbook=tmp_path / "patch.yml",
        inventory=tmp_path / "inventory.ini",
        working_directory=tmp_path,
        timeout_seconds=15,
        extra_args=("--check",),
    )


def fake_popen(*communicate, returncode=0, on_start=None):
    """Return a Popen replacement and the process its calls return.

    Each item of ``communicate`` is one result of ``process.communicate()``:
    an ``(stdout, stderr)`` tuple or an exception to raise.
    """
    process = MagicMock(returncode=returncode)
    process.__enter__.return_value = process
    process.communicate.side_effect = list(communicate)

    def start(*_args, **kwargs):
        """Return the configured process mock for a Popen call."""
        if on_start is not None:
            on_start(kwargs)
        return process

    return Mock(side_effect=start), process


def test_run_job_persists_success_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist successful output and metrics with the expected command."""
    monkeypatch.delenv("ANSIBLE_CALLBACKS_ENABLED", raising=False)

    def write_metrics(kwargs):
        """Write a valid metrics payload to the callback's configured path."""
        Path(kwargs["env"]["PLAYBOOK_SCHEDULER_METRICS_FILE"]).write_text(
            json.dumps({"hosts_total": 1, "hosts_changed": 1}),
            encoding="utf-8",
        )

    popen, process = fake_popen(("changed=1", ""), on_start=write_metrics)
    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(make_job(tmp_path), tmp_path / "runs")

    saved = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
    assert result["status"] == "success"
    assert saved["stdout"] == "changed=1"
    assert saved["return_code"] == 0
    assert saved["metrics"] == {"hosts_total": 1, "hosts_changed": 1}
    assert "ANSIBLE_CALLBACKS_ENABLED" not in popen.call_args.kwargs["env"]
    assert result["run_id"] in next((tmp_path / "runs").glob("*.json")).name
    assert process.communicate.call_args.kwargs["timeout"] == 15
    assert popen.call_args.args[0] == [
        "ansible-playbook",
        "-i",
        str(tmp_path / "inventory.ini"),
        str(tmp_path / "patch.yml"),
        "--check",
    ]


def test_run_job_preserves_enabled_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave user-configured enabled callbacks unchanged in the child process."""
    monkeypatch.setenv("ANSIBLE_CALLBACKS_ENABLED", "profile_tasks,timer")
    popen, _ = fake_popen(("", ""))

    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        run_job(make_job(tmp_path), tmp_path / "runs")

    assert (
        popen.call_args.kwargs["env"]["ANSIBLE_CALLBACKS_ENABLED"]
        == "profile_tasks,timer"
    )


def test_nonzero_exit_is_recorded_as_failed(tmp_path: Path) -> None:
    """Record a non-zero Ansible exit status as a failed run."""
    popen, _ = fake_popen(("", "play failed"), returncode=2)
    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(make_job(tmp_path), tmp_path / "runs")

    assert result["status"] == "failed"
    assert result["return_code"] == 2


def test_timeout_terminates_ansible_gracefully(tmp_path: Path) -> None:
    """Terminate a timed-out process and record its captured output."""
    popen, process = fake_popen(
        subprocess.TimeoutExpired("ansible-playbook", 15), ("started", "")
    )
    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(make_job(tmp_path), tmp_path / "runs")

    assert result["status"] == "failed"
    assert result["stdout"] == "started"
    assert result["return_code"] is None
    assert isinstance(result["error"], str)
    assert "Timed out" in str(result["error"])
    process.terminate.assert_called_once()
    process.kill.assert_not_called()


def test_timeout_kills_ansible_that_ignores_terminate(tmp_path: Path) -> None:
    """Kill the process when it remains alive after the graceful timeout."""
    popen, process = fake_popen(
        subprocess.TimeoutExpired("ansible-playbook", 15),
        subprocess.TimeoutExpired("ansible-playbook", STOP_GRACE_SECONDS),
        ("started", ""),
    )
    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(make_job(tmp_path), tmp_path / "runs")

    assert isinstance(result["error"], str)
    assert "Timed out" in str(result["error"])
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_interrupt_stops_ansible_and_is_raised(tmp_path: Path) -> None:
    """Stop Ansible when the caller is interrupted and re-raise the interrupt."""
    popen, process = fake_popen(KeyboardInterrupt(), ("", ""))
    with (
        patch("playbook_scheduler.runner.subprocess.Popen", popen),
        pytest.raises(KeyboardInterrupt),
    ):
        run_job(make_job(tmp_path), tmp_path / "runs")

    process.terminate.assert_called_once()


def test_start_error_is_recorded(tmp_path: Path) -> None:
    """Record an operating-system error when Ansible cannot be started."""
    popen = Mock(side_effect=FileNotFoundError("ansible-playbook"))
    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(make_job(tmp_path), tmp_path / "runs")

    assert result["status"] == "failed"
    assert isinstance(result["error"], str)
    assert "Could not start ansible-playbook" in str(result["error"])


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script")
def test_timeout_stops_real_process(tmp_path: Path) -> None:
    """Stop a real long-running process promptly after its job timeout."""
    virtualenv = tmp_path / ".venv"
    executable = virtualenv / "bin" / "ansible-playbook"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho started\nexec sleep 30\n", encoding="utf-8")
    executable.chmod(0o755)
    job = replace(make_job(tmp_path), timeout_seconds=1, ansible_venv=virtualenv)

    started = time.monotonic()
    result = run_job(job, tmp_path / "runs")

    assert time.monotonic() - started < 10
    assert result["error"] == "Timed out after 1 seconds."
    assert result["stdout"] == "started\n"


def test_run_job_uses_configured_virtualenv(tmp_path: Path) -> None:
    """Run the virtualenv executable with its paths in the child environment."""
    virtualenv = tmp_path / "project" / ".venv"
    executable = virtualenv / "bin" / "ansible-playbook"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    job = make_job(tmp_path)
    job = Job(
        name=job.name,
        cron=job.cron,
        playbook=job.playbook,
        inventory=job.inventory,
        working_directory=job.working_directory,
        timeout_seconds=job.timeout_seconds,
        extra_args=job.extra_args,
        ansible_venv=virtualenv,
    )
    popen, _ = fake_popen(("ok", ""))

    with patch("playbook_scheduler.runner.subprocess.Popen", popen):
        result = run_job(job, tmp_path / "runs")

    assert result["status"] == "success"
    assert popen.call_args.args[0][0] == str(executable)
    assert popen.call_args.kwargs["env"]["VIRTUAL_ENV"] == str(virtualenv)
    assert popen.call_args.kwargs["env"]["PATH"].split(":")[0] == str(virtualenv / "bin")
