"""Ansible callback plugin that saves host and failed-task metrics as JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ansible.plugins.callback import CallbackBase

if TYPE_CHECKING:
    from ansible.executor.stats import AggregateStats
    from ansible.executor.task_result import TaskResult

DOCUMENTATION = r"""
name: playbook_scheduler_metrics
type: aggregate
short_description: Save structured host and failed-task metrics for Playbook Scheduler
description:
  - Writes host statistics and failed task names to the path in
    C(PLAYBOOK_SCHEDULER_METRICS_FILE).
requirements:
  - Enabled by Playbook Scheduler for each playbook process.
"""


class CallbackModule(CallbackBase):
    """Collect per-host counters and failed tasks for Playbook Scheduler.

    The metrics are written to the file named in the environment variable
    ``PLAYBOOK_SCHEDULER_METRICS_FILE`` when the playbook finishes.
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "playbook_scheduler_metrics"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        """Initialize the plugin and read the metrics file path."""
        super().__init__()
        self._failed_tasks: dict[tuple[str, str], dict[str, str]] = {}
        self._metrics_file = os.environ.get("PLAYBOOK_SCHEDULER_METRICS_FILE")

    def v2_runner_on_failed(
        self, result: TaskResult, ignore_errors: bool = False
    ) -> None:
        """Record a failed task unless its errors are ignored.

        Args:
            result: Result of the failed task on one host.
            ignore_errors: Whether the task has ``ignore_errors`` set.
        """
        if ignore_errors:
            return
        host_name = result._host.get_name()
        task_name = result._task.get_name().strip()
        self._failed_tasks[(host_name, task_name)] = {
            "host": host_name,
            "task": task_name,
        }

    def v2_playbook_on_stats(self, stats: AggregateStats) -> None:
        """Write the collected metrics when the playbook finishes.

        Args:
            stats: Aggregated per-host statistics of the playbook run.
        """
        hosts: list[dict[str, Any]] = []
        for host_name in sorted(stats.processed):
            summary = stats.summarize(host_name)
            hosts.append(
                {
                    "name": host_name,
                    "ok": summary["ok"],
                    "changed": summary["changed"],
                    "failed": summary["failures"],
                    "unreachable": summary["unreachable"],
                    "skipped": summary["skipped"],
                    "ignored": summary["ignored"],
                }
            )

        metrics = {
            "hosts_total": len(hosts),
            "hosts_ok": sum(
                host["ok"] > 0 and host["failed"] == 0 and host["unreachable"] == 0
                for host in hosts
            ),
            "hosts_changed": sum(host["changed"] > 0 for host in hosts),
            "hosts_failed": sum(host["failed"] > 0 for host in hosts),
            "hosts_unreachable": sum(host["unreachable"] > 0 for host in hosts),
            "tasks_failed": len(self._failed_tasks),
            "hosts": hosts,
            "failed_tasks": sorted(
                self._failed_tasks.values(), key=lambda task: (task["host"], task["task"])
            ),
        }

        if self._metrics_file is None:
            self._display.warning(
                "Playbook Scheduler metrics file is not configured; host metrics were not saved."
            )
            return

        target = Path(self._metrics_file)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(metrics, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
