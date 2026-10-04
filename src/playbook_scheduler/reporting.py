"""Render the HTML report from the stored run records."""

import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from playbook_scheduler.files import atomic_write_text

DEFAULT_OUTPUT_LINES = 200
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
    atomic_write_text(
        report_path,
        template.render(
            runs=load_runs(runs_directory),
            tail=partial(tail_output, max_lines=max_output_lines),
            max_output_lines=max_output_lines,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return report_path
