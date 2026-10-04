"""Combine running a job with retention and report generation."""

from typing import Any

from playbook_scheduler.config import AppConfig, Job
from playbook_scheduler.files import REPORT_LOCK_NAME, LockBusy, file_lock, lock_path
from playbook_scheduler.reporting import generate_report
from playbook_scheduler.retention import remove_expired_runs
from playbook_scheduler.runner import record_skipped_run, run_job


class JobAlreadyRunning(RuntimeError):
    """Raised when another thread or process is already running the job."""


def execute_job(job: Job, config: AppConfig) -> dict[str, Any]:
    """Run a job unless it is already running, then refresh the report.

    If the job is already running, a run with status ``skipped`` is recorded
    instead and the report is refreshed before the exception is raised.

    Args:
        job: Job to run.
        config: Configuration with the output directories.

    Returns:
        The run record as returned by ``run_job``.

    Raises:
        JobAlreadyRunning: If another thread or process holds the job's lock.
    """
    try:
        # Non-blocking: a second run of the same job is skipped, not queued.
        with file_lock(lock_path(config.runs_directory, job.name), blocking=False):
            result = run_job(job, config.runs_directory)
    except LockBusy as error:
        message = f"Job {job.name} is already running."
        record_skipped_run(job, config.runs_directory, f"Skipped: {message}")
        refresh_report(config)
        raise JobAlreadyRunning(message) from error
    refresh_report(config)
    return result


def refresh_report(config: AppConfig) -> str:
    """Remove expired run records and regenerate the HTML report.

    Args:
        config: Configuration with the output directories and retention.

    Returns:
        Path of the written report.
    """
    # Blocking: retention and report generation of parallel runs take turns.
    with file_lock(lock_path(config.runs_directory, REPORT_LOCK_NAME)):
        remove_expired_runs(config.runs_directory, config.retention_days)
        return str(
            generate_report(
                config.runs_directory,
                config.reports_directory,
                config.report_output_lines,
            )
        )
