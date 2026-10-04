"""Provide the ``playbook-scheduler`` command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playbook_scheduler.config import ConfigError, find_ansible_playbook, load_config
from playbook_scheduler.scheduler import configure_logging, run_scheduler
from playbook_scheduler.service import JobAlreadyRunning, execute_job, refresh_report


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="playbook-scheduler",
        description="Schedule Ansible playbooks and generate run reports.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.yaml"),
        help="Path to the YAML configuration file (default: config/config.yaml).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate configuration and referenced paths.")
    run_parser = commands.add_parser("run", help="Run a configured job immediately.")
    run_parser.add_argument("job_name", help="Configured job name.")
    commands.add_parser("report", help="Refresh the HTML report.")
    commands.add_parser("serve", help="Start the blocking cron scheduler.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command given on the command line.

    Args:
        argv: Command-line arguments without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        Exit code: ``0`` on success, ``1`` if a run failed, ``2`` for
        configuration errors, a missing ``ansible-playbook``, or an unknown
        job, ``3`` if the job is already running.
    """
    arguments = _parser().parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.command == "validate":
            missing = False
            for job in config.jobs:
                executable = find_ansible_playbook(job)
                if executable is not None:
                    print(f"{job.name}: {executable}")
                elif job.ansible_venv is not None:
                    missing = True
                    print(
                        f"{job.name}: ansible-playbook is not executable: "
                        f"{job.ansible_playbook_executable}",
                        file=sys.stderr,
                    )
                else:
                    missing = True
                    print(
                        f"{job.name}: ansible-playbook not found on PATH; "
                        "install ansible-core or set ansible_venv.",
                        file=sys.stderr,
                    )
            if missing:
                return 2
            print(f"Configuration is valid: {config.config_path}")
            return 0
        if arguments.command == "run":
            job = next((job for job in config.jobs if job.name == arguments.job_name), None)
            if job is None:
                print(f"Unknown job: {arguments.job_name}", file=sys.stderr)
                return 2
            try:
                result = execute_job(job, config)
            except JobAlreadyRunning as error:
                print(str(error), file=sys.stderr)
                return 3
            print(
                f"{result['job']}: {result['status']} "
                f"(run {result['run_id']}, {result['duration_seconds']}s)"
            )
            if result["status"] == "success":
                return 0
            if result["error"]:
                print(f"Error: {result['error']}", file=sys.stderr)
            if result["stdout"]:
                print(f"Ansible stdout:\n{result['stdout']}", file=sys.stderr)
            if result["stderr"]:
                print(f"Ansible stderr:\n{result['stderr']}", file=sys.stderr)
            return 1
        if arguments.command == "report":
            print(f"Report written to {refresh_report(config)}")
            return 0
        if arguments.command == "serve":
            configure_logging()
            run_scheduler(config)
            return 0
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
