"""Run a named expectation suite against a pandas batch and summarise the outcome."""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

os.environ.setdefault("TQDM_DISABLE", "1")  # GX metric progress bars

import great_expectations as gx  # noqa: E402
from great_expectations.data_context.types.base import ProgressBarsConfig  # noqa: E402

from loanlens.quality.suites import SUITES  # noqa: E402


class DataQualityError(RuntimeError):
    """Raised when a batch fails validation; the load for that batch is rolled back."""

    def __init__(self, outcome: ValidationOutcome):
        self.outcome = outcome
        super().__init__(outcome.describe())


@dataclass
class ValidationOutcome:
    suite: str
    batch_id: str
    success: bool
    row_count: int
    results: list[dict] = field(default_factory=list)
    validated_at: datetime = field(default_factory=datetime.now)

    @property
    def failures(self) -> list[dict]:
        return [r for r in self.results if not r["success"]]

    @property
    def pass_rate(self) -> float:
        return sum(r["success"] for r in self.results) / max(len(self.results), 1)

    def describe(self) -> str:
        if self.success:
            return f"{self.suite} [{self.batch_id}]: all {len(self.results)} checks passed"
        lines = [f"{self.suite} [{self.batch_id}]: {len(self.failures)} of {len(self.results)} checks failed"]
        for f in self.failures:
            lines.append(f"  - {f['expectation']}({f['column'] or ''}): "
                         f"{f['unexpected_count']} unexpected, e.g. {f['examples']}")
        return "\n".join(lines)

    def to_frame(self, run_id: str, source_period: str) -> pd.DataFrame:
        df = pd.DataFrame(self.results)
        df.insert(0, "run_id", run_id)
        df.insert(1, "batch_id", self.batch_id)
        df.insert(2, "source_period", source_period)
        df.insert(3, "suite", self.suite)
        df["validated_at"] = self.validated_at
        df["examples"] = df["examples"].astype(str)
        return df


@lru_cache(maxsize=1)
def _context():
    ctx = gx.get_context(mode="ephemeral")
    ctx.variables.progress_bars = ProgressBarsConfig(globally=False, metric_calculations=False)
    source = ctx.data_sources.add_pandas("loanlens")
    suites = {}
    for name, factory in SUITES.items():
        suite = ctx.suites.add(gx.ExpectationSuite(name=name))
        for expectation in factory():
            suite.add_expectation(expectation)
        batch_def = source.add_dataframe_asset(name=name).add_batch_definition_whole_dataframe("batch")
        suites[name] = (suite, batch_def)
    return suites


def validate(df: pd.DataFrame, suite_name: str, batch_id: str) -> ValidationOutcome:
    suite, batch_def = _context()[suite_name]
    batch = batch_def.get_batch(batch_parameters={"dataframe": df})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # pandas regex match-group notice
        result = batch.validate(suite, result_format="SUMMARY")
    rows = []
    for r in result.results:
        cfg = r.expectation_config
        res = r.result or {}
        rows.append({
            "expectation": cfg.type,
            "column": cfg.kwargs.get("column") or ",".join(cfg.kwargs.get("column_list", []) or []),
            "success": bool(r.success),
            "element_count": res.get("element_count", len(df)),
            "unexpected_count": res.get("unexpected_count", 0 if r.success else None),
            "unexpected_percent": res.get("unexpected_percent"),
            "examples": list(res.get("partial_unexpected_list", []) or [])[:5]
                        or ([res["observed_value"]] if "observed_value" in res and not r.success else []),
        })
    return ValidationOutcome(suite_name, batch_id, bool(result.success), len(df), rows)


def save_outcome(outcome: ValidationOutcome, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{outcome.suite}__{outcome.batch_id}.json"
    payload = {
        "suite": outcome.suite, "batch_id": outcome.batch_id, "success": outcome.success,
        "row_count": outcome.row_count, "validated_at": outcome.validated_at.isoformat(),
        "results": outcome.results,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
