"""Render the HTML report from the stored run records."""

import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, select_autoescape

from playbook_scheduler.files import atomic_write_text

DEFAULT_OUTPUT_LINES = 200
DEFAULT_PAGE_SIZE = 50
# Ansible prints module results with -v as one JSON line, which can be huge.
MAX_LINE_CHARS = 1000


def tail_output(text: str, max_lines: int) -> tuple[str, int]:
    """Shorten process output for the report.

    Keeps the last ``max_lines`` lines and cuts every line after
    ``MAX_LINE_CHARS`` characters. The run record keeps the full output.

    Args:
        text: Complete standard output or standard error.
        max_lines: Number of trailing lines to keep.

    Returns:
        The shortened text and the number of omitted leading lines.
    """
    lines = text.splitlines()
    omitted = max(len(lines) - max_lines, 0)
    shown = [
        line if len(line) <= MAX_LINE_CHARS else f"{line[:MAX_LINE_CHARS]} …[line cut]"
        for line in lines[omitted:]
    ]
    return "\n".join(shown), omitted


def load_runs(runs_directory: Path) -> list[dict[str, Any]]:
    """Load all run records, newest first.

    Args:
        runs_directory: Directory with the JSON run records.

    Returns:
        The parsed records; empty if the directory does not exist.

    Raises:
        TypeError: If a record file does not contain a JSON object.

    Each record gets an extra ``record_file`` key with its file name.
    """
    runs: list[dict[str, Any]] = []
    if not runs_directory.exists():
        return runs
    for run_file in sorted(runs_directory.glob("*.json"), reverse=True):
        with run_file.open(encoding="utf-8") as source:
            run = json.load(source)
        if not isinstance(run, dict):
            raise TypeError(f"Run record must contain a JSON object: {run_file}")
        run["record_file"] = run_file.name
        runs.append(run)
    return runs


def _write_full_output_logs(runs: list[dict[str, Any]], reports_directory: Path) -> None:
    """Write complete output files linked from the HTML report."""
    logs_directory = reports_directory / "run-logs"
    logs_directory.mkdir(parents=True, exist_ok=True)
    active_logs: set[str] = set()
    for run in runs:
        filename = Path(run["record_file"]).with_suffix(".txt").name
        active_logs.add(filename)
        run["full_output_href"] = f"run-logs/{quote(filename, safe='')}"

        metadata = [
            f"Job: {run.get('job', '')}",
            f"Status: {run.get('status', '')}",
            f"Started: {run.get('started_at', '')}",
        ]
        if run.get("command"):
            metadata.append(f"Command: {json.dumps(run['command'], ensure_ascii=False)}")
        sections = ["\n".join(metadata)]
        sections.extend(
            f"--- {label} ---\n{run.get(field) or ''}"
            for label, field in (
                ("Standard output", "stdout"),
                ("Standard error", "stderr"),
            )
        )
        if run.get("error"):
            sections.append(f"--- Scheduler error ---\n{run['error']}")
        atomic_write_text(logs_directory / filename, "\n\n".join(sections) + "\n")

    # Keep retention of generated output files in sync with retained run records.
    for log_file in logs_directory.glob("*.txt"):
        if log_file.name not in active_logs:
            log_file.unlink()


def _add_host_change_data(runs: list[dict[str, Any]]) -> list[str]:
    """Add per-run changed-host names and return all hosts in the history."""
    host_names: set[str] = set()
    for run in runs:
        metrics = run.get("metrics")
        hosts = metrics.get("hosts", []) if isinstance(metrics, dict) else []
        changed_hosts: list[str] = []
        if isinstance(hosts, list):
            for host in hosts:
                if not isinstance(host, dict) or not isinstance(host.get("name"), str):
                    continue
                name = host["name"]
                host_names.add(name)
                changed = host.get("changed")
                if isinstance(changed, int) and changed > 0:
                    changed_hosts.append(name)
        run["changed_hosts"] = changed_hosts
    return sorted(host_names, key=str.casefold)


def generate_report(
    runs_directory: Path,
    reports_directory: Path,
    max_output_lines: int = DEFAULT_OUTPUT_LINES,
) -> Path:
    """Render all run records into ``index.html``.

    Args:
        runs_directory: Directory with the JSON run records.
        reports_directory: Directory for the report; created if missing.
        max_output_lines: Number of trailing output lines shown per run.

    Returns:
        Path of the written report.
    """
    reports_directory.mkdir(parents=True, exist_ok=True)
    environment = Environment(
        loader=PackageLoader("playbook_scheduler", "templates"),
        autoescape=select_autoescape(("html", "xml", "j2")),
    )
    template = environment.get_template("report.html.j2")
    report_path = reports_directory / "index.html"
    runs = load_runs(runs_directory)
    hosts = _add_host_change_data(runs)
    _write_full_output_logs(runs, reports_directory)
    atomic_write_text(
        report_path,
        template.render(
            runs=runs,
            tail=partial(tail_output, max_lines=max_output_lines),
            max_output_lines=max_output_lines,
            default_page_size=DEFAULT_PAGE_SIZE,
            hosts=hosts,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return report_path
