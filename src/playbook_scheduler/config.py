"""Load and validate the YAML configuration of Playbook Scheduler."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from apscheduler.triggers.cron import CronTrigger


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True)
class Job:
    """A configured playbook run with its schedule and execution settings.

    Attributes:
        name: Unique job identifier used by the CLI, scheduler, and reports.
        cron: Five-field cron expression interpreted in the global time zone.
        playbook: Absolute path to the Ansible playbook.
        inventory: Absolute path to the Ansible inventory file or directory.
        working_directory: Working directory of the ``ansible-playbook`` process.
        timeout_seconds: Maximum runtime before the process is terminated.
        extra_args: Additional command-line arguments for ``ansible-playbook``.
        ansible_venv: Optional virtual environment that provides Ansible.
    """

    name: str
    cron: str
    playbook: Path
    inventory: Path
    working_directory: Path
    timeout_seconds: int
    extra_args: tuple[str, ...]
    ansible_venv: Path | None = None

    @property
    def ansible_playbook_executable(self) -> Path | None:
        """Return the ``ansible-playbook`` executable inside ``ansible_venv``.

        Returns:
            The platform-specific executable path, or ``None`` when no
            virtual environment is configured and ``PATH`` is used instead.
        """
        if self.ansible_venv is None:
            return None
        executable_directory = "Scripts" if os.name == "nt" else "bin"
        executable_name = (
            "ansible-playbook.exe" if os.name == "nt" else "ansible-playbook"
        )
        return self.ansible_venv / executable_directory / executable_name


def find_ansible_playbook(job: Job) -> Path | None:
    """Return the ``ansible-playbook`` executable that ``job`` would run.

    With ``ansible_venv`` this is the executable in that environment, otherwise
    the first ``ansible-playbook`` on the ``PATH`` of the current process.

    Args:
        job: Job to check.

    Returns:
        Path of the executable, or ``None`` if it is missing or not executable.
    """
    executable = job.ansible_playbook_executable
    found = shutil.which(str(executable) if executable else "ansible-playbook")
    return Path(found) if found else None


@dataclass(frozen=True)
class AppConfig:
    """The complete, validated application configuration.

    Attributes:
        config_path: Absolute path of the loaded configuration file.
        timezone: IANA time zone used for cron schedules.
        runs_directory: Directory for the JSON records of every run.
        reports_directory: Directory for the generated HTML report.
        retention_days: Number of days run records are kept.
        jobs: All configured jobs.
        max_parallel_jobs: Maximum number of jobs the scheduler runs at once.
        report_output_lines: Number of trailing output lines per run that the
            HTML report shows.
    """

    config_path: Path
    timezone: str
    runs_directory: Path
    reports_directory: Path
    retention_days: int
    jobs: tuple[Job, ...]
    max_parallel_jobs: int = 10
    report_output_lines: int = 200


def _mapping(value: object, context: str) -> dict[str, object]:
    """Return ``value`` if it is a mapping with string keys.

    Args:
        value: Parsed YAML value to check.
        context: Location of the value, used in the error message.

    Returns:
        The unchanged mapping.

    Raises:
        ConfigError: If ``value`` is not a mapping with string keys.
    """
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{context} must be a YAML mapping.")
    return value


def _allowed_keys(
    mapping: dict[str, object], allowed: set[str], context: str
) -> None:
    """Reject keys of ``mapping`` that are not in ``allowed``.

    Args:
        mapping: Mapping whose keys are checked.
        allowed: Permitted key names.
        context: Location of the mapping, used in the error message.

    Raises:
        ConfigError: If ``mapping`` contains an unsupported key.
    """
    unexpected = sorted(mapping.keys() - allowed)
    if unexpected:
        raise ConfigError(f"{context} has unsupported keys: {', '.join(unexpected)}.")


def _required_string(mapping: dict[str, object], key: str, context: str) -> str:
    """Return the non-empty string stored under ``key``.

    Args:
        mapping: Mapping that contains the value.
        key: Name of the required key.
        context: Location of the mapping, used in the error message.

    Returns:
        The value without leading and trailing whitespace.

    Raises:
        ConfigError: If the key is missing or not a non-empty string.
    """
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string.")
    return value.strip()


def _resolve_path(value: str, base: Path) -> Path:
    """Resolve ``value`` to an absolute path.

    Args:
        value: Path from the configuration; ``~`` is expanded.
        base: Directory that relative paths are resolved against.

    Returns:
        The absolute, normalized path.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _optional_path(
    mapping: dict[str, object], key: str, default: str, base: Path
) -> Path:
    """Return the resolved path under ``key``, or ``default`` if it is missing.

    Args:
        mapping: Mapping that may contain the value.
        key: Name of the optional key.
        default: Path used when the key is missing.
        base: Directory that relative paths are resolved against.

    Returns:
        The absolute, normalized path.

    Raises:
        ConfigError: If the value is present but not a non-empty string.
    """
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty path.")
    return _resolve_path(value.strip(), base)


def load_config(config_path: Path | str) -> AppConfig:
    """Load, validate, and resolve the configuration file.

    Relative output directories and working directories are resolved from the
    directory of the configuration file; relative playbook, inventory, and
    virtual environment paths from the job's working directory.

    Args:
        config_path: Path of the YAML configuration file.

    Returns:
        The validated configuration with absolute paths.

    Raises:
        ConfigError: If the file cannot be read, is not valid YAML, or
            contains invalid values or paths that do not exist.
    """
    path = Path(config_path).expanduser().resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read configuration file {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in configuration file {path}: {error}") from error

    data = _mapping(raw, "Configuration")
    _allowed_keys(
        data,
        {
            "timezone",
            "runs_directory",
            "reports_directory",
            "retention_days",
            "max_parallel_jobs",
            "report_output_lines",
            "jobs",
        },
        "Configuration",
    )

    timezone = data.get("timezone", "UTC")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("timezone must be a non-empty IANA time zone name.")
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ConfigError(f"Unknown time zone: {timezone}.") from error

    retention_days = data.get("retention_days", 30)
    if type(retention_days) is not int or retention_days < 1:
        raise ConfigError("retention_days must be a positive integer.")

    max_parallel_jobs = data.get("max_parallel_jobs", 10)
    if type(max_parallel_jobs) is not int or max_parallel_jobs < 1:
        raise ConfigError("max_parallel_jobs must be a positive integer.")

    report_output_lines = data.get("report_output_lines", 200)
    if type(report_output_lines) is not int or report_output_lines < 1:
        raise ConfigError("report_output_lines must be a positive integer.")

    base_directory = path.parent
    runs_directory = _optional_path(data, "runs_directory", "runs", base_directory)
    reports_directory = _optional_path(
        data, "reports_directory", "reports", base_directory
    )

    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ConfigError("jobs must be a non-empty YAML list.")

    jobs: list[Job] = []
    names: set[str] = set()
    for index, raw_job in enumerate(raw_jobs):
        context = f"jobs[{index}]"
        job_data = _mapping(raw_job, context)
        _allowed_keys(
            job_data,
            {
                "name",
                "cron",
                "playbook",
                "inventory",
                "working_directory",
                "timeout_seconds",
                "extra_args",
                "ansible_venv",
            },
            context,
        )

        name = _required_string(job_data, "name", context)
        if not name[0].isalnum() or any(
            not (character.isalnum() or character in "._-") for character in name
        ):
            raise ConfigError(
                f"{context}.name may contain only letters, numbers, dots, "
                "underscores, and hyphens, and must start with a letter or number."
            )
        if name in names:
            raise ConfigError(f"Duplicate job name: {name}.")
        names.add(name)

        cron = _required_string(job_data, "cron", context)
        try:
            CronTrigger.from_crontab(cron, timezone=timezone)
        except ValueError as error:
            raise ConfigError(f"{context}.cron is invalid: {error}") from error

        working_directory_value = job_data.get("working_directory", ".")
        if not isinstance(working_directory_value, str) or not working_directory_value.strip():
            raise ConfigError(f"{context}.working_directory must be a path.")
        working_directory = _resolve_path(
            working_directory_value.strip(), base_directory
        )
        if not working_directory.is_dir():
            raise ConfigError(
                f"{context}.working_directory does not exist or is not a directory: "
                f"{working_directory}"
            )

        ansible_venv_value = job_data.get("ansible_venv")
        ansible_venv = None
        if ansible_venv_value is not None:
            if not isinstance(ansible_venv_value, str) or not ansible_venv_value.strip():
                raise ConfigError(f"{context}.ansible_venv must be a path.")
            ansible_venv = _resolve_path(
                ansible_venv_value.strip(), working_directory
            )
            if not ansible_venv.is_dir():
                raise ConfigError(
                    f"{context}.ansible_venv does not exist or is not a directory: "
                    f"{ansible_venv}"
                )

        playbook_value = _required_string(job_data, "playbook", context)
        inventory_value = _required_string(job_data, "inventory", context)
        playbook = _resolve_path(playbook_value, working_directory)
        inventory = _resolve_path(inventory_value, working_directory)
        if not playbook.is_file():
            raise ConfigError(f"{context}.playbook is not a file: {playbook}")
        if not inventory.exists():
            raise ConfigError(f"{context}.inventory does not exist: {inventory}")

        timeout_seconds = job_data.get("timeout_seconds", 3600)
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ConfigError(f"{context}.timeout_seconds must be a positive integer.")

        extra_args = job_data.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(
            isinstance(argument, str) and argument for argument in extra_args
        ):
            raise ConfigError(f"{context}.extra_args must be a list of non-empty strings.")

        job = Job(
            name=name,
            cron=cron,
            playbook=playbook,
            inventory=inventory,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
            extra_args=tuple(extra_args),
            ansible_venv=ansible_venv,
        )
        if job.ansible_playbook_executable is not None and not (
            job.ansible_playbook_executable.is_file()
        ):
            raise ConfigError(
                f"{context}.ansible_venv does not contain ansible-playbook: "
                f"{job.ansible_playbook_executable}"
            )
        jobs.append(job)

    return AppConfig(
        config_path=path,
        timezone=timezone,
        runs_directory=runs_directory,
        reports_directory=reports_directory,
        retention_days=retention_days,
        jobs=tuple(jobs),
        max_parallel_jobs=max_parallel_jobs,
        report_output_lines=report_output_lines,
    )
