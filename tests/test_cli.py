import os
from pathlib import Path

import pytest

from playbook_scheduler import cli
from playbook_scheduler.config import AppConfig, Job


def test_run_command_displays_process_start_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    job = Job(
        name="local_ping",
        cron="0 2 * * *",
        playbook=tmp_path / "ping.yml",
        inventory=tmp_path / "hosts.ini",
        working_directory=tmp_path,
        timeout_seconds=30,
        extra_args=(),
    )
    config = AppConfig(
        config_path=tmp_path / "config.yaml",
        timezone="UTC",
        runs_directory=tmp_path / "runs",
        reports_directory=tmp_path / "reports",
        retention_days=30,
        jobs=(job,),
    )
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(
        cli,
        "execute_job",
        lambda *_: {
            "job": "local_ping",
            "status": "failed",
            "run_id": "test-run",
            "duration_seconds": 0.001,
            "error": "Could not start ansible-playbook: executable not found",
            "stdout": "",
            "stderr": "",
        },
    )

    exit_code = cli.main(["run", "local_ping"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "local_ping: failed" in captured.out
    assert "Could not start ansible-playbook: executable not found" in captured.err


def write_validate_config(tmp_path: Path, venv: str = "") -> Path:
    (tmp_path / "ping.yml").write_text("---\n", encoding="utf-8")
    (tmp_path / "hosts.ini").write_text("localhost\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""jobs:
  - name: local_ping
    cron: "0 2 * * *"
    playbook: ping.yml
    inventory: hosts.ini
{venv}""",
        encoding="utf-8",
    )
    return config_path


def test_validate_reports_ansible_playbook_missing_on_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = write_validate_config(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    exit_code = cli.main(["--config", str(config_path), "validate"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "local_ping: ansible-playbook not found on PATH" in captured.err
    assert "Configuration is valid" not in captured.out


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX file permissions")
def test_validate_checks_ansible_playbook_in_virtualenv(
    tmp_path: Path, capsys
) -> None:
    executable = tmp_path / ".venv" / "bin" / "ansible-playbook"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_path = write_validate_config(tmp_path, "    ansible_venv: .venv\n")

    assert cli.main(["--config", str(config_path), "validate"]) == 2
    assert "ansible-playbook is not executable" in capsys.readouterr().err

    executable.chmod(0o755)

    assert cli.main(["--config", str(config_path), "validate"]) == 0
    captured = capsys.readouterr()
    assert f"local_ping: {executable}" in captured.out
    assert "Configuration is valid" in captured.out
