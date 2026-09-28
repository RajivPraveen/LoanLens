"""Programmatic dbt invocation (same project, profile and warehouse as the CLI)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from loanlens.config import Settings

log = logging.getLogger(__name__)


class DbtError(RuntimeError):
    pass


def dbt_env(settings: Settings) -> dict[str, str]:
    return {
        "LOANLENS_WAREHOUSE": str(settings.warehouse_path),
        "DBT_LOG_PATH": str(settings.artifacts_dir / "dbt_logs"),
        "DBT_TARGET_PATH": str(settings.artifacts_dir / "dbt_target"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }


def dbt_args(settings: Settings) -> list[str]:
    return ["--project-dir", str(settings.dbt_project_dir),
            "--profiles-dir", str(settings.dbt_project_dir)]


def dbt_executable() -> str:
    candidate = Path(sys.executable).with_name("dbt")
    return str(candidate) if candidate.exists() else "dbt"


def run_dbt(settings: Settings, *args: str) -> None:
    """Run a dbt command, e.g. ``run_dbt(s, "build", "--exclude", "tag:post_model")``.

    dbt runs in a subprocess so its DuckDB connection never collides with ours and global
    dbt state does not leak between invocations.
    """
    env = {**os.environ, **dbt_env(settings)}
    command = [dbt_executable(), *args, *dbt_args(settings)]
    log.info("dbt %s", " ".join(args))
    result = subprocess.run(command, env=env, cwd=settings.dbt_project_dir)
    if result.returncode != 0:
        raise DbtError(f"dbt {' '.join(args)} failed (exit {result.returncode}); see output above")


def build_core(settings: Settings, full_refresh: bool = False) -> None:
    """Seeds, staging, core star schema and analytics marts, with all tests."""
    args = ["build", "--exclude", "tag:post_model"]
    run_dbt(settings, *(args + (["--full-refresh"] if full_refresh else [])))


def build_reporting(settings: Settings) -> None:
    """Reporting models that join the model outputs written to the `ml` schema."""
    run_dbt(settings, "build", "--select", "tag:post_model")
