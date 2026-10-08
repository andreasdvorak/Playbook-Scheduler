"""Tests for report generation, output truncation, and run retention."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playbook_scheduler.reporting import MAX_LINE_CHARS, generate_report, tail_output
from playbook_scheduler.retention import remove_expired_runs


def test_report_escapes_output_and_shows_empty_state(tmp_path: Path) -> None:
    """Escape untrusted output and render both populated and empty reports."""
    runs = tmp_path / "runs"
    reports = tmp_path / "reports"
    runs.mkdir()
    (runs / "run.json").write_text(
        json.dumps(
            {
                "job": "<script>alert(1)</script>",
                "status": "failed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "duration_seconds": 1.25,
                "return_code": 2,
                "stdout": "<unsafe>",
                "stderr": "",
                "error": None,
                "metrics": {
                    "hosts_total": 1,
                    "hosts_ok": 0,
                    "hosts_changed": 1,
                    "hosts_failed": 1,
                    "hosts_unreachable": 0,
                    "tasks_failed": 1,
                    "hosts": [
                        {
                            "name": "localhost",
                            "ok": 2,
                            "changed": 1,
                            "failed": 1,
                            "unreachable": 0,
                            "skipped": 0,
                            "ignored": 0,
                        }
                    ],
                    "failed_tasks": [
                        {"host": "localhost", "task": "Install package"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    report_path = generate_report(runs, reports)
    html = report_path.read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html
    assert "&lt;unsafe&gt;" in html
    assert 'id="job-filter"' in html
    assert 'data-job="&lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert 'id="status-filter"' in html
    assert 'data-status="failed"' in html
    assert 'id="changed-host-filter"' in html
    assert '<option value="localhost">localhost</option>' in html
    assert 'data-changed-hosts="[&#34;localhost&#34;]"' in html
    assert 'id="start-date-filter"' in html
    assert 'id="end-date-filter"' in html
    assert 'id="page-size"' in html
    assert 'id="previous-page"' in html
    assert 'id="next-page"' in html
    assert 'id="reset-filters"' in html
    assert 'id="run-count" aria-live="polite"' in html
    assert 'data-date="2026-01-01"' in html
    assert "Hosts: 1" in html
    assert "localhost" in html
    assert "Install package on localhost" in html
    assert "No runs match the selected filters." in html
    assert "matchesJob && matchesStatus && matchesChangedHost &&" in html
    assert "matchingRows.slice(firstIndex, lastIndex)" in html
    assert "Showing ${matchingRows.length ? firstIndex + 1 : 0}" in html
    assert 'resetFilters.addEventListener("click"' in html
    assert 'View complete run output (stdout and stderr)' in html

    (runs / "run.json").unlink()
    empty_report = generate_report(runs, reports).read_text(encoding="utf-8")
    assert "No Ansible runs have been recorded yet." in empty_report
    assert not (reports / "run-logs" / "run.txt").exists()


def test_report_filters_runs_by_host_with_changes(tmp_path: Path) -> None:
    """List changed hosts and include only matching runs in the host filter."""
    runs = tmp_path / "runs"
    write_run(
        runs,
        "changed.json",
        metrics={
            "hosts": [
                {"name": "app-1", "changed": 1},
                {"name": "db-1", "changed": 0},
            ]
        },
    )
    write_run(
        runs,
        "unchanged.json",
        metrics={"hosts": [{"name": "app-1", "changed": 0}]},
    )
    write_run(runs, "missing-metrics.json", metrics=None)

    html = generate_report(runs, tmp_path / "reports").read_text(encoding="utf-8")

    assert '<option value="app-1">app-1</option>' in html
    assert '<option value="db-1">db-1</option>' in html
    assert 'data-changed-hosts="[&#34;app-1&#34;]"' in html
    assert 'data-changed-hosts="[&#34;db-1&#34;]"' not in html
    assert 'data-changed-hosts="[]"' in html
    assert "changedHosts.includes(changedHostFilter.value)" in html


def test_retention_removes_only_runs_older_than_cutoff(tmp_path: Path) -> None:
    """Delete expired run records while preserving recent ones."""
    runs = tmp_path / "runs"
    runs.mkdir()
    expired = runs / "expired.json"
    recent = runs / "recent.json"
    expired.write_text("{}", encoding="utf-8")
    recent.write_text("{}", encoding="utf-8")
    now = datetime.now(timezone.utc)
    old_timestamp = (now - timedelta(days=31)).timestamp()
    os.utime(expired, (old_timestamp, old_timestamp))

    removed = remove_expired_runs(runs, 30, now=now)

    assert removed == 1
    assert not expired.exists()
    assert recent.exists()


def write_run(runs: Path, name: str, **fields) -> None:
    """Write a run record with defaults that individual tests can override."""
    runs.mkdir(exist_ok=True)
    record = {
        "job": "ping",
        "status": "success",
        "started_at": "2026-01-01T00:00:00+00:00",
        "duration_seconds": 1.0,
        "return_code": 0,
        "stdout": "",
        "stderr": "",
        "error": None,
        "metrics": None,
        **fields,
    }
    (runs / name).write_text(json.dumps(record), encoding="utf-8")


def test_tail_output_keeps_last_lines_and_cuts_long_lines() -> None:
    """Keep only requested trailing lines and truncate oversized lines."""
    text = "\n".join(f"line {number}" for number in range(1, 11))

    assert tail_output(text, 3) == ("line 8\nline 9\nline 10", 7)
    assert tail_output(text, 20) == (text, 0)
    shown, omitted = tail_output("x" * (MAX_LINE_CHARS + 5), 5)
    assert omitted == 0
    assert shown == "x" * MAX_LINE_CHARS + " …[line cut]"


def test_report_shows_only_last_output_lines(tmp_path: Path) -> None:
    """Display only the configured tail of captured command output."""
    stdout = "\n".join(f"output line {number}" for number in range(1, 301))
    write_run(tmp_path / "runs", "20260101_ping.json", stdout=stdout, stderr="warning")

    html = generate_report(tmp_path / "runs", tmp_path / "reports", 100).read_text(
        encoding="utf-8"
    )

    assert "output line 300" in html
    assert "output line 201\n" in html
    assert "output line 200\n" not in html
    assert "Standard output (last 100 lines)" in html
    assert "200 earlier lines omitted" in html
    assert 'href="run-logs/20260101_ping.txt"' in html
    full_output = (
        tmp_path / "reports" / "run-logs" / "20260101_ping.txt"
    ).read_text(encoding="utf-8")
    assert "output line 1" in full_output
    assert "output line 300" in full_output
    assert "--- Standard error ---\nwarning" in full_output
    # Short output is shown completely and without a note.
    assert "<summary>Standard error</summary>" in html


def test_report_paginates_large_run_histories(tmp_path: Path) -> None:
    """Offer paging controls instead of displaying a large run history at once."""
    runs = tmp_path / "runs"
    for number in range(75):
        write_run(runs, f"{number:03}.json", job=f"job-{number}")

    html = generate_report(runs, tmp_path / "reports").read_text(encoding="utf-8")

    assert html.count('class="run run-row"') == 75
    assert '<option value="50" selected>50</option>' in html
    assert 'aria-label="Run pages"' in html
    assert "Page ${currentPage} of ${Math.max(pageCount, 1)}" in html
    assert "nextPage.addEventListener" in html


def test_report_shows_skipped_runs(tmp_path: Path) -> None:
    """Render skipped runs with the skipped status and explanatory message."""
    write_run(tmp_path / "runs", "a.json")
    write_run(
        tmp_path / "runs",
        "b.json",
        status="skipped",
        duration_seconds=0.0,
        return_code=None,
        error="Skipped: Job ping is already running.",
    )

    html = generate_report(tmp_path / "runs", tmp_path / "reports").read_text(
        encoding="utf-8"
    )

    assert 'class="badge skipped"' in html
    assert '<option value="skipped">Skipped</option>' in html
    assert 'id="stat-skipped">1<' in html
    assert '<p class="error note">Skipped: Job ping is already running.</p>' in html
    assert 'data-status="skipped" data-date="2026-01-01" data-duration=""' in html
