"""Schedule the configured jobs and reload them when the configuration changes."""

import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from playbook_scheduler.config import AppConfig, ConfigError, Job, load_config
from playbook_scheduler.runner import stop_running_jobs
from playbook_scheduler.service import JobAlreadyRunning, execute_job, refresh_report

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"
# The journal adds its own timestamp, so journal lines carry only the level.
JOURNAL_LOG_FORMAT = "%(levelname)s %(message)s"
# Syslog priorities that systemd reads from a "<N>" prefix on each line.
SYSLOG_PRIORITIES = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}
CONFIG_CHECK_INTERVAL_SECONDS = 30
# Job names must start with a letter or number, so this ID cannot collide.
CONFIG_WATCHER_JOB_ID = "__config_watcher__"
# The watcher gets its own thread so long playbook runs cannot delay reloads.
CONFIG_WATCHER_EXECUTOR = "config_watcher"

logger = logging.getLogger(__name__)


class JournalFormatter(logging.Formatter):
    """Prefix every line with its syslog priority for the systemd journal.

    systemd stores each line of standard error as its own journal entry, so
    tracebacks and other multi-line messages get the prefix on every line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a record and prefix each line with its syslog priority.

        Args:
            record: Log record to format.

        Returns:
            Formatted lines, each starting with ``<N>``.
        """
        priority = SYSLOG_PRIORITIES.get(record.levelno)
        if priority is None:
            priority = 3 if record.levelno > logging.WARNING else 6
        return "\n".join(
            f"<{priority}>{line}" for line in super().format(record).splitlines()
        )


def _stderr_is_journal() -> bool:
    """Return whether standard error is connected to the systemd journal.

    systemd sets ``JOURNAL_STREAM`` to the device and inode of the journal
    stream. Comparing them with standard error ignores the variable when it was
    only inherited, for example by a process started from a service.
    """
    journal_stream = os.environ.get("JOURNAL_STREAM", "")
    try:
        device, inode = (int(value) for value in journal_stream.split(":"))
        status = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return (status.st_dev, status.st_ino) == (device, inode)


def configure_logging() -> None:
    """Log to standard error with timestamps and levels.

    Under systemd the journal timestamps each line, so the own timestamp is
    left out and each line starts with its syslog priority instead. This lets
    ``journalctl -p err`` find failed runs.

    APScheduler is limited to warnings; at INFO it would log every run of the
    configuration watcher. Job starts and ends are logged by this module.
    """
    handler = logging.StreamHandler()
    if _stderr_is_journal():
        handler.setFormatter(JournalFormatter(JOURNAL_LOG_FORMAT))
    else:
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _file_signature(path: Path) -> tuple[int, int] | None:
    """Return values that change whenever the file at ``path`` changes.

    Args:
        path: File to inspect.

    Returns:
        Modification time in nanoseconds and size, or ``None`` if the file
        cannot be read.
    """
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def run_scheduled_job(job: Job, config: AppConfig) -> None:
    """Run a job from the scheduler and log its start and outcome.

    A run of a job that is still running is recorded as skipped.

    Args:
        job: Job to run.
        config: Configuration that was active when the job was scheduled.
    """
    logger.info("Starting job %s", job.name)
    try:
        result = execute_job(job, config)
    except JobAlreadyRunning as error:
        logger.warning("Skipped scheduled run: %s", error)
        return
    log = logger.info if result["status"] == "success" else logger.error
    log(
        "Job %s finished: %s after %ss (run %s)%s",
        job.name,
        result["status"],
        result["duration_seconds"],
        result["run_id"],
        f": {result['error']}" if result["error"] else "",
    )


class ConfigWatcher:
    """Reloads the configuration and reschedules jobs when the file changes."""

    def __init__(self, scheduler: BaseScheduler, config: AppConfig) -> None:
        """Remember the scheduler, configuration, and current file state.

        Args:
            scheduler: Scheduler whose jobs are managed.
            config: Currently active configuration.
        """
        self.scheduler = scheduler
        self.config = config
        self._signature = _file_signature(config.config_path)

    def schedule_jobs(self) -> None:
        """Make the scheduled jobs match the active configuration.

        Jobs that are no longer configured are removed; all configured jobs
        are added or replaced. The configuration watcher is left untouched.
        """
        configured = {job.name for job in self.config.jobs}
        for scheduled in self.scheduler.get_jobs():
            if scheduled.id != CONFIG_WATCHER_JOB_ID and scheduled.id not in configured:
                scheduled.remove()
        for job in self.config.jobs:
            self.scheduler.add_job(
                run_scheduled_job,
                trigger=CronTrigger.from_crontab(job.cron, timezone=self.config.timezone),
                args=[job, self.config],
                id=job.name,
                name=job.name,
                # The second instance only reaches execute_job to record a
                # skipped run; the job lock keeps it from starting Ansible.
                max_instances=2,
                coalesce=True,
                misfire_grace_time=3600,
                replace_existing=True,
            )

    def check(self) -> bool:
        """Reload the configuration if the file has changed.

        An invalid file is reported and the previous configuration stays
        active until the file changes again.

        Returns:
            ``True`` if a new configuration was loaded and applied.
        """
        signature = _file_signature(self.config.config_path)
        # A missing file is usually an editor replacing it; wait for the new one.
        if signature is None or signature == self._signature:
            return False
        self._signature = signature
        try:
            config = load_config(self.config.config_path)
        except ConfigError as error:
            logger.error(
                "Configuration reload failed, keeping previous configuration: %s",
                error,
            )
            return False
        if config.max_parallel_jobs != self.config.max_parallel_jobs:
            logger.warning(
                "max_parallel_jobs changed; the new value takes effect after a restart."
            )
        self.config = config
        self.schedule_jobs()
        refresh_report(config)
        logger.info(
            "Configuration reloaded from %s: %s",
            config.config_path,
            ", ".join(job.name for job in config.jobs),
        )
        return True


class StopSignalHandler:
    """Shut down the scheduler and running jobs on SIGTERM or SIGINT."""

    def __init__(self, scheduler: BaseScheduler) -> None:
        """Remember the scheduler to shut down.

        Args:
            scheduler: Scheduler that is stopped on the first signal.
        """
        self.scheduler = scheduler
        self.stopping = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        """Handle a stop signal.

        The first signal stops scheduling new runs and sends SIGTERM to the
        running Ansible processes. A second signal kills them with SIGKILL.

        Args:
            signum: Number of the received signal.
            frame: Stack frame at the time of the signal; unused.
        """
        name = signal.Signals(signum).name
        if self.stopping:
            logger.warning("Received %s again; killing running Ansible processes.", name)
            stop_running_jobs(force=True)
            return
        self.stopping = True
        logger.info("Received %s; stopping running jobs.", name)
        stop_running_jobs()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)


def run_scheduler(config: AppConfig) -> None:
    """Schedule all jobs and the configuration watcher, then block.

    Returns after SIGTERM or SIGINT, once interrupted runs have been recorded.

    Args:
        config: Initial configuration.
    """
    refresh_report(config)
    jobs_executor = ThreadPoolExecutor(config.max_parallel_jobs)
    scheduler = BlockingScheduler(
        timezone=ZoneInfo(config.timezone),
        executors={
            "default": jobs_executor,
            CONFIG_WATCHER_EXECUTOR: ThreadPoolExecutor(1),
        },
    )
    watcher = ConfigWatcher(scheduler, config)
    watcher.schedule_jobs()
    scheduler.add_job(
        watcher.check,
        trigger="interval",
        seconds=CONFIG_CHECK_INTERVAL_SECONDS,
        id=CONFIG_WATCHER_JOB_ID,
        name="config watcher",
        executor=CONFIG_WATCHER_EXECUTOR,
        max_instances=1,
        coalesce=True,
        # A check that starts a little late is harmless; do not log a warning.
        misfire_grace_time=CONFIG_CHECK_INTERVAL_SECONDS,
    )
    handler = StopSignalHandler(scheduler)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handler)
    if not handler.stopping:
        scheduler.start()
    # start() returns after the handler's shutdown(); wait until the
    # interrupted runs have written their records and the report.
    jobs_executor.shutdown(wait=True)
    logger.info("Scheduler stopped.")
