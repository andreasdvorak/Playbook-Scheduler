from pathlib import Path

import pytest

from playbook_scheduler.config import ConfigError, load_config


def write_config(directory: Path, cron: str = "0 2 * * *") -> Path:
    (directory / "playbooks").mkdir()
    (directory / "inventory").mkdir()
    (directory / "playbooks" / "patch.yml").write_text("---\n", encoding="utf-8")
    (directory / "inventory" / "prod.ini").write_text("localhost\n", encoding="utf-8")
    config_path = directory / "config.yaml"
    config_path.write_text(
        f"""
timezone: Europe/Berlin
retention_days: 30
jobs:
  - name: patch_linux
    cron: "{cron}"
    playbook: playbooks/patch.yml
    inventory: inventory/prod.ini
""",
        encoding="utf-8",
    )
    return config_path


def test_load_config_resolves_paths_relative_to_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.jobs[0].playbook == tmp_path / "playbooks" / "patch.yml"
    assert config.jobs[0].inventory == tmp_path / "inventory" / "prod.ini"
    assert config.retention_days == 30
    assert config.jobs[0].timeout_seconds == 3600


def test_invalid_cron_is_reported(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, "not a cron")

    with pytest.raises(ConfigError, match="cron is invalid"):
        load_config(config_path)


def test_missing_playbook_is_reported(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    (tmp_path / "playbooks" / "patch.yml").unlink()

    with pytest.raises(ConfigError, match="playbook is not a file"):
        load_config(config_path)


def test_ansible_venv_resolves_from_working_directory(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    virtualenv = tmp_path / ".venv"
    executable = virtualenv / "bin" / "ansible-playbook"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    inventory: inventory/prod.ini\n",
            "    inventory: inventory/prod.ini\n    ansible_venv: .venv\n",
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.jobs[0].ansible_venv == virtualenv
    assert config.jobs[0].ansible_playbook_executable == executable


def test_ansible_venv_must_contain_ansible_playbook(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    inventory: inventory/prod.ini\n",
            "    inventory: inventory/prod.ini\n    ansible_venv: .venv\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="does not contain ansible-playbook"):
        load_config(config_path)
