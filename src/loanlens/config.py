"""Settings: config/loanlens.yaml with named profiles, plus the project path layout.

Two roots are distinguished:
- ``repo``: where the code, config and dbt project live.
- ``workspace``: where generated data, artifacts and reports are written. Defaults to the
  repo; set ``LOANLENS_WORKSPACE`` to redirect (the test-suite uses a temp directory).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml


def _find_repo() -> Path:
    """Directory holding config/ and dbt/: $LOANLENS_REPO, the source checkout, or the cwd."""
    candidates = [os.environ.get("LOANLENS_REPO"), Path(__file__).resolve().parents[2], Path.cwd()]
    for c in candidates:
        if c and (Path(c) / "config" / "loanlens.yaml").exists():
            return Path(c).resolve()
    raise FileNotFoundError("Cannot locate the LoanLens repo (config/loanlens.yaml); set LOANLENS_REPO")


REPO_ROOT = _find_repo()


def _deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass(frozen=True)
class Settings:
    profile: str
    cfg: dict[str, Any]
    repo: Path
    workspace: Path

    def __getitem__(self, key: str) -> Any:
        return self.cfg[key]

    # ---- paths -------------------------------------------------------------------------
    @cached_property
    def root(self) -> Path:
        """Output root: the workspace, or a profile sub-folder (e.g. real/ for Freddie data)
        so a real-data run never mixes with the synthetic demo."""
        sub = (self.cfg.get("paths") or {}).get("output_subdir")
        return self.workspace / sub if sub else self.workspace

    @cached_property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_freddie_dir(self) -> Path:
        return self.data_dir / "raw" / "freddie"

    @property
    def raw_fred_dir(self) -> Path:
        return self.data_dir / "raw" / "fred"

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "warehouse" / "loanlens.duckdb"

    @property
    def alerts_dir(self) -> Path:
        return self.data_dir / "alerts"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def figures_dir(self) -> Path:
        return self.reports_dir / "figures"

    @property
    def powerbi_dir(self) -> Path:
        return self.root / "powerbi" / "data"

    @property
    def dbt_project_dir(self) -> Path:
        return self.repo / "dbt"

    @property
    def state_reference_path(self) -> Path:
        return self.dbt_project_dir / "seeds" / "state_reference.csv"

    def ensure_dirs(self) -> None:
        for path in (
            self.raw_freddie_dir,
            self.raw_fred_dir,
            self.warehouse_path.parent,
            self.alerts_dir,
            self.artifacts_dir,
            self.figures_dir,
            self.powerbi_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(profile: str | None = None, workspace: str | Path | None = None) -> Settings:
    profile = profile or os.environ.get("LOANLENS_PROFILE", "freddie")
    config_path = Path(os.environ.get("LOANLENS_CONFIG", REPO_ROOT / "config" / "loanlens.yaml"))
    raw = yaml.safe_load(config_path.read_text())
    profiles = raw.get("profiles", {})
    if profile not in profiles:
        raise ValueError(f"Unknown profile {profile!r}; choose from {sorted(profiles)}")
    cfg = _deep_merge(raw["default"], profiles[profile])
    ws = Path(workspace or os.environ.get("LOANLENS_WORKSPACE", REPO_ROOT)).resolve()
    settings = Settings(profile=profile, cfg=cfg, repo=REPO_ROOT, workspace=ws)
    settings.ensure_dirs()
    return settings
