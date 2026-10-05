"""Run ``ansible-playbook`` for a job and store the result as a JSON record."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playbook_scheduler.config import Job
from playbook_scheduler.files import atomic_write_text

STOP_GRACE_SECONDS = 30

_running_processes: set[subprocess.Popen[str]] = set()
_running_lock = threading.Lock()
_stop_requested = threading.Event()


def stop_running_jobs(force: bool = False) -> None:
    """Stop all running ``ansible-playbook`` processes and refuse new ones.

    Used when the scheduler shuts down. Each interrupted run is still recorded,
    as failed with the error ``Interrupted by shutdown.``.

    Args:
        force: Send SIGKILL instead of SIGTERM. Only meant for processes that
            did not react to SIGTERM, because SIGKILL leaves Ansible's worker
            processes running (see ``_stop_process``).
    """
    with _running_lock:
        _stop_requested.set()
        for process in _running_processes:
            if force:
                process.kill()
            else:
                process.terminate()


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop ``process`` and collect the output it produced so far.

    Ansible runs its workers in their own sessions, so they are not reached
    through the process group, and SIGKILL on ``ansible-playbook`` would leave
    them and their tasks running. SIGTERM lets Ansible stop them itself;
    SIGKILL is only sent if it has not exited after ``STOP_GRACE_SECONDS``.

    Args:
        process: Running ``ansible-playbook`` process with captured output.

    Returns:
        Standard output and standard error of the process.
    """
    process.terminate()
    try:
        return process.communicate(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def _write_result(runs_directory: Path, result: dict[str, Any]) -> Path:
    """Write ``result`` to a uniquely named JSON file.

    Args:
        runs_directory: Directory for run records; created if missing.
        result: Run record as built by ``run_job``.

    Returns:
        Path of the written file.
    """
    runs_directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", result["job"])
    timestamp = result["started_at"].replace(":", "").replace("-", "")
    destination = runs_directory / f"{timestamp}_{safe_name}_{result['run_id']}.json"
    atomic_write_text(
        destination, json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    return destination


def _command(job: Job) -> list[str]:
    """Return the ``ansible-playbook`` command line for ``job``.

    Args:
        job: Job to build the command for.

    Returns:
        Executable and arguments.
    """
    executable = job.ansible_playbook_executable
    command_name = str(executable) if executable is not None else "ansible-playbook"
    return [
        command_name,
        "-i",
        str(job.inventory),
        str(job.playbook),
        *job.extra_args,
    ]


def _new_result(job: Job, started: datetime) -> dict[str, Any]:
    """Return a run record for ``job`` with default values.

    Args:
        job: Job the record belongs to.
        started: Start time of the run.

    Returns:
        A record with status ``failed`` that the caller completes.
    """
    return {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "job": job.name,
        "status": "failed",
        "command": _command(job),
        "started_at": started.isoformat(),
        "ended_at": "",
        "duration_seconds": 0.0,
        "return_code": None,
        "stdout": "",
        "stderr": "",
        "error": None,
        "metrics": None,
        "metrics_error": None,
    }


def record_skipped_run(job: Job, runs_directory: Path, reason: str) -> dict[str, Any]:
    """Record that a run of ``job`` was skipped without starting Ansible.

    Args:
        job: Job whose run was skipped.
        runs_directory: Directory where the JSON run record is written.
        reason: Explanation stored in the record's ``error`` field.

    Returns:
        The run record with status ``skipped``.
    """
    now = datetime.now(timezone.utc)
    result = _new_result(job, now)
    result["status"] = "skipped"
    result["error"] = reason
    result["ended_at"] = now.isoformat()
    _write_result(runs_directory, result)
    return result


def _ansible_environment(job: Job) -> dict[str, str]:
    """Build the process environment with the scheduler callback plugin available."""
    environment = os.environ.copy()
    if job.ansible_venv is not None:
        executable_directory = "Scripts" if os.name == "nt" else "bin"
        virtualenv_bin = str(job.ansible_venv / executable_directory)
        environment["PATH"] = os.pathsep.join(
            [virtualenv_bin, environment.get("PATH", "")]
        )
        environment["VIRTUAL_ENV"] = str(job.ansible_venv)

    metrics_directory = Path(__file__).parent / "callback_plugins"
    callback_paths = [
        str(metrics_directory),
        *filter(None, environment.get("ANSIBLE_CALLBACK_PLUGINS", "").split(os.pathsep)),
    ]
    environment["ANSIBLE_CALLBACK_PLUGINS"] = os.pathsep.join(callback_paths)
    return environment


def _start_process(
    job: Job, result: dict[str, Any], environment: dict[str, str]
) -> subprocess.Popen[str] | None:
    """Start Ansible, recording and returning ``None`` on start errors."""
    try:
        return subprocess.Popen(
            result["command"],
            cwd=job.working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except OSError as error:
        result["error"] = f"Could not start ansible-playbook: {error}"
        return None


def _run_process(
    process: subprocess.Popen[str], job: Job, result: dict[str, Any]
) -> bool:
    """Collect process output and return whether it completed normally."""
    process_completed = False
    with process as running_process:
        with _running_lock:
            _running_processes.add(running_process)
            # Started just after stop_running_jobs() went through the set.
            if _stop_requested.is_set():
                running_process.terminate()
        try:
            try:
                stdout, stderr = running_process.communicate(timeout=job.timeout_seconds)
            except subprocess.TimeoutExpired:
                stdout, stderr = _stop_process(running_process)
                result["error"] = f"Timed out after {job.timeout_seconds} seconds."
            except BaseException:
                # Ctrl+C during ``playbook-scheduler run``: do not leave Ansible running.
                _stop_process(running_process)
                raise
            else:
                result["return_code"] = running_process.returncode
                if _stop_requested.is_set() and running_process.returncode != 0:
                    result["error"] = "Interrupted by shutdown."
                else:
                    process_completed = True
                    result["status"] = (
                        "success" if running_process.returncode == 0 else "failed"
                    )
            result["stdout"] = stdout
            result["stderr"] = stderr
        finally:
            with _running_lock:
                _running_processes.discard(running_process)
    return process_completed


def _read_metrics(
    metrics_file: Path, process_completed: bool, result: dict[str, Any]
) -> None:
    """Load callback metrics into the run record or explain why they are absent."""
    if metrics_file.is_file():
        try:
            metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
            if not isinstance(metrics, dict):
                raise TypeError("Ansible callback metrics must be a JSON object.")
            result["metrics"] = metrics
        except (OSError, json.JSONDecodeError, TypeError) as error:
            result["metrics_error"] = f"Could not read Ansible callback metrics: {error}"
    elif process_completed:
        result["metrics_error"] = (
            "Ansible did not produce host metrics. Check that the configured "
            "Ansible version supports callback plugins and that the callback "
            "plugin is discoverable and loaded."
        )


def run_job(job: Job, runs_directory: Path) -> dict[str, Any]:
    """Run the job's playbook once and record the outcome.

    The ``playbook_scheduler_metrics`` callback plugin is made discoverable for
    the process and its host metrics are added to the record. Failures,
    timeouts, and start errors are recorded instead of raised.

    Args:
        job: Job to run.
        runs_directory: Directory where the JSON run record is written.

    Returns:
        The run record, including status, timing, command, exit code,
        captured output, and host metrics.
    """
    started = datetime.now(timezone.utc)
    environment = _ansible_environment(job)
    result = _new_result(job, started)

    with tempfile.TemporaryDirectory(prefix="playbook-scheduler-metrics-") as temporary_dir:
        metrics_file = Path(temporary_dir) / "metrics.json"
        environment["PLAYBOOK_SCHEDULER_METRICS_FILE"] = str(metrics_file)
        process = _start_process(job, result, environment)
        process_completed = (
            _run_process(process, job, result) if process is not None else False
        )
        _read_metrics(metrics_file, process_completed, result)

    ended = datetime.now(timezone.utc)
    result["ended_at"] = ended.isoformat()
    result["duration_seconds"] = round((ended - started).total_seconds(), 3)

    _write_result(runs_directory, result)
    return result
