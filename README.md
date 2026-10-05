# Playbook Scheduler

A small Python service for scheduling Ansible playbooks and keeping a
file-based history of their results. Each execution is stored as JSON and
rendered into a single HTML report. No database, web server, or Ansible Python
SDK is required; playbooks are started with the `ansible-playbook` command.

## Features

- YAML configuration with validation of job names, cron expressions, paths,
  timeout values, and time zone.
- Manual execution of a configured job or scheduled execution with APScheduler.
- One uniquely named JSON record per run, including timestamps, duration,
  command, exit code, standard output, and standard error.
- Per-host task-result counters and failed task names captured with an Ansible
  callback plugin.
- Regenerated HTML report with run history, including skipped runs; long
  output is shortened in the report but kept in full in the JSON records.
- Timestamped log of job starts, results, and scheduler events while `serve`
  runs.
- Retention of old run records by age.
- Parallel execution of different jobs with a configurable limit; a job that
  is still running is never started a second time, not even by another
  process.

## Requirements

- Python 3.10 or newer.
- Ansible installed and `ansible-playbook` available on `PATH`.
- Access to the playbooks, inventory, credentials, and any required Ansible
  collections or roles.

## Installation

```bash
git clone https://github.com/andreasdvorak/Ansible_Runner.git
cd Ansible_Runner
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Installing `ansible-core` in the same environment provides the
`ansible-playbook` executable required to run the example jobs.
But you need Python 3.12 or newer.

```bash
python -m pip install -e ".[ansible]"
```

## Configure jobs

Edit [`config/config.yaml`](config/config.yaml) to point to your playbooks and
inventories. The checked-in example runs Ansible's `ping` module against
localhost:

```yaml
timezone: Europe/Berlin
runs_directory: ../runs
reports_directory: ../reports
retention_days: 30

jobs:
  - name: local_ping
    cron: "0 2 * * *"
    playbook: examples/playbooks/ping.yml
    inventory: examples/inventory/hosts.ini
    working_directory: ..
    timeout_seconds: 3600
    extra_args: []
```

### Configuration reference

| Variable | Scope | Required / default | Description |
| --- | --- | --- | --- |
| `timezone` | Global | Optional; defaults to `UTC` | IANA time zone used to interpret job schedules, for example `Europe/Berlin`. |
| `runs_directory` | Global | Optional; defaults to `runs` | Directory for per-run JSON records. Relative paths are resolved from the directory containing the configuration file. |
| `reports_directory` | Global | Optional; defaults to `reports` | Directory for generated HTML reports. Relative paths are resolved from the directory containing the configuration file. |
| `retention_days` | Global | Optional; defaults to `30` | Positive number of days to keep run JSON files. Older records are removed during service/report activity. |
| `report_output_lines` | Global | Optional; defaults to `200` | Number of trailing lines of standard output and standard error shown per run in the HTML report. Lines longer than 1000 characters are cut. The JSON records always keep the full output. |
| `max_parallel_jobs` | Global | Optional; defaults to `10` | Maximum number of different jobs the scheduler runs at the same time. Further due jobs wait for a free slot. Changes take effect after restarting `serve`. |
| `jobs` | Global | Required; at least one job | List of jobs the scheduler can run. |
| `jobs[].name` | Job | Required | Unique job identifier used by the CLI and reports. Use letters, numbers, `.`, `_`, or `-`; the first character must be a letter or number. |
| `jobs[].cron` | Job | Required | Five-field cron schedule, such as `"0 2 * * *"`. Interpreted in the global `timezone`. |
| `jobs[].playbook` | Job | Required | Path to the Ansible playbook. Relative paths are resolved from `working_directory`. |
| `jobs[].inventory` | Job | Required | Path to the Ansible inventory file or directory. Relative paths are resolved from `working_directory`. |
| `jobs[].working_directory` | Job | Optional; defaults to the configuration file's directory | Working directory for the Ansible process. Relative paths are resolved from the configuration file's directory. |
| `jobs[].timeout_seconds` | Job | Optional; defaults to `3600` | Positive maximum runtime for the playbook in seconds. When it is exceeded, `ansible-playbook` is asked to stop (SIGTERM) so it can end its running tasks, and is killed only if it has not exited after 30 more seconds. |
| `jobs[].extra_args` | Job | Optional; defaults to `[]` | List of additional command-line arguments passed to `ansible-playbook`. Use one string per argument. |
| `jobs[].ansible_venv` | Job | Optional; defaults to Playbook Scheduler's inherited `PATH` | Path to the Ansible project's virtual environment. Relative paths are resolved from `working_directory`; the environment must contain `ansible-playbook`. |

The `jobs[]` notation means a field on each item in the `jobs` list. Output
directories are resolved relative to the configuration file. The job's
`working_directory` is also resolved relative to that file, while relative
playbook, inventory, and `ansible_venv` paths are resolved relative to the
working directory.

### Use an Ansible project's own virtual environment

Playbook Scheduler and an Ansible project can use separate Python environments. Set
`working_directory` to the Ansible project root and `ansible_venv` to that
project's virtual environment directory:

```yaml
jobs:
  - name: production_deploy
    cron: "0 2 * * *"
    working_directory: /srv/ansible-project
    playbook: playbooks/deploy.yml
    inventory: inventory/production.ini
    ansible_venv: /srv/ansible-project/.venv
    timeout_seconds: 3600
    extra_args: []
```

`ansible_venv` may also be a relative path; it is resolved from
`working_directory`. Playbook Scheduler invokes `ansible-playbook` from that
environment and adds its `bin/` directory to the child process's `PATH`, so
Ansible and its installed collections and dependencies are used for that job.
On Windows, the executable is resolved under `Scripts/`. The environment must
already contain Ansible (`ansible-core`) and any required collections. If
`ansible_venv` is omitted, Playbook Scheduler uses `ansible-playbook` from its inherited
`PATH`.

Validate the configuration and referenced paths before starting:

```bash
playbook-scheduler validate
```

Besides the configuration and the playbook and inventory paths, `validate`
checks that every job finds an executable `ansible-playbook` and prints its
path per job. Jobs with `ansible_venv` use the executable in that environment;
all other jobs use the first `ansible-playbook` on `PATH`. The check uses the
`PATH` of the shell that runs `validate`, so run it as the service account and
with the same environment as `serve`. If an executable is missing, `validate`
exits with code `2`.

The supplied configuration points to files under `examples/` and can be
validated as-is. Replace those paths with your own when adapting the sample.

Use `--config` to select a different configuration file:

```bash
playbook-scheduler --config /etc/playbook-scheduler/config.yaml validate
```

## Run and view reports

Run a job immediately:

```bash
playbook-scheduler run local_ping
```

Generate or refresh the HTML report without starting a playbook:

```bash
playbook-scheduler report
```

The scheduler adds its metrics callback to Ansible's callback plugin search
path without overriding `callbacks_enabled` from `ansible.cfg`.

The report includes client-side filters for job name, status, and a UTC date
range. Filters can be combined; the job-name search is case-insensitive and
updates as you type. The report shows the number of matching runs and provides
a button to reset all filters. Expand a run's host metrics to see per-host
`ok`, `changed`, `failed`, `unreachable`, `skipped`, and `ignored` counters,
plus failed task names and their hosts. The metrics callback only includes task
and host names for failures; the existing stdout/stderr capture is unchanged.

To keep the report small, it shows only the last `report_output_lines` lines
(default 200) of each run's standard output and standard error, and cuts lines
longer than 1000 characters, such as the JSON results Ansible prints with
`-v`. A note above the shortened output names the JSON record that contains
the full output.

Runs that were skipped because the job was still running are listed with the
status `skipped` and can be selected in the status filter. They do not count
towards the success rate or the average duration.

With the checked-in configuration, outputs are stored in `runs/` and
`reports/index.html`. If the output paths are omitted, they default to
`runs/` and `reports/` next to the configuration file. A failed
Ansible command is still recorded as a failed run; the manual `run` command
returns a non-zero exit code for that run. Process output is retained in JSON
and displayed in the report, so protect these directories as they may contain
sensitive information.

Old JSON run records are removed when the service starts, when a job completes,
and when a report is generated. Set `retention_days` to a positive number of
days.

## Start the scheduler

```bash
playbook-scheduler serve
```

Keep the process running under a service manager such as systemd for unattended
scheduling. Run one scheduler instance for a given set of output directories.

Different jobs run in parallel, up to `max_parallel_jobs` at a time. The same
job never runs twice at once: if it is still running when its next fire time
arrives, or when it is started with `playbook-scheduler run`, the new run is
skipped and not queued. The skipped run is recorded with the status `skipped`
and appears in the report. `run` then exits with code `3`; the scheduler logs
`Skipped scheduled run: …`. This also applies across processes, because Playbook Scheduler uses lock files in
`runs_directory/.locks/`. Missed fire times within the one-hour misfire grace
period are coalesced into a single run.

To stop the scheduler, send SIGTERM (`systemctl stop`, `kill`) or press Ctrl+C.
Playbook Scheduler then stops scheduling new runs and asks every running
`ansible-playbook` to stop, so Ansible can end its running tasks. Interrupted
runs are recorded as failed with the error `Interrupted by shutdown.`, the
report is updated, and the process exits. If Ansible does not stop, a second
signal kills the remaining `ansible-playbook` processes.

The scheduler checks the configuration file every 30 seconds and reloads it
when it changes: new jobs are added, removed jobs are unscheduled, and changed
jobs are rescheduled without a restart. Runs that are already in progress
finish with their previous settings. If the changed file is invalid, the error
is logged and the previous configuration stays active until the file changes
again.

`serve` writes a log line with timestamp and level to standard error for each
job start and result and for scheduler events, for example:

```text
2026-10-03 19:21:00+0200 INFO Starting job slow
2026-10-03 19:22:00+0200 WARNING Skipped scheduled run: Job slow is already running.
2026-10-03 19:22:41+0200 INFO Job slow finished: success after 101.2s (run 6a80…)
```

Failed runs are logged at level `ERROR`. Under systemd the log is available
with `journalctl -u playbook-scheduler.service`. There Playbook Scheduler leaves out its own
timestamp, because the journal adds one, and passes the level to the journal as
syslog priority. `journalctl -u playbook-scheduler.service -p err` therefore shows
only failed runs and other errors, and `-p warning` adds skipped runs.

### Example systemd service

Create a dedicated system account and install Playbook Scheduler in a stable location.
For example, save the following as
`/etc/systemd/system/playbook-scheduler.service`:

```ini
[Unit]
Description=Playbook Scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=playbook-scheduler
Group=playbook-scheduler
WorkingDirectory=/opt/playbook-scheduler
ExecStart=/opt/playbook-scheduler/.venv/bin/playbook-scheduler --config /opt/playbook-scheduler/config/config.yaml serve
Restart=on-failure
RestartSec=5
# Send SIGTERM only to Playbook Scheduler, which stops Ansible and records the runs.
KillMode=mixed
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
```

`KillMode=mixed` sends SIGTERM only to Playbook Scheduler instead of to every process
of the service at once. Playbook Scheduler then stops Ansible itself and records the
interrupted runs. If the service has not stopped after `TimeoutStopSec`,
systemd kills all remaining processes, including Ansible's workers.

Make sure the service account can read the Playbook Scheduler configuration, playbooks,
inventory, and credentials, and can write to the configured run and report
directories. Set `ansible_venv` for each job to the Ansible project's virtual
environment; it does not need to be activated by systemd. Then load and enable
the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now playbook-scheduler.service
sudo systemctl status playbook-scheduler.service
sudo journalctl -u playbook-scheduler.service
```

## CLI reference

```text
playbook-scheduler [--config PATH] validate
playbook-scheduler [--config PATH] run JOB_NAME
playbook-scheduler [--config PATH] report
playbook-scheduler [--config PATH] serve
```

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | `run`: the playbook run failed or timed out |
| `2` | Invalid configuration, missing `ansible-playbook` (`validate`), or unknown job |
| `3` | `run`: the job is already running |
