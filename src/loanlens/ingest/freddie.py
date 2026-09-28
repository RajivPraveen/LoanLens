"""Incremental, idempotent loader for Freddie Mac loan-level files.

Accepted inputs in the raw directory (zipped or not; zips may hold one or many periods):
    orig_YYYYQn.txt + perf_YYYYQn.txt                      Standard dataset, Release 47+
    historical_data_YYYYQn.txt + historical_data_time_YYYYQn.txt   Standard, earlier releases
    sample_orig_YYYY.txt + sample_perf_YYYY.txt            Sample dataset (annual; older releases: _svcg_)
Each file's layout (Release 47 or legacy) is detected from its field count.

For each source period the loader:
    1. fingerprints the file(s) with SHA-256 and skips periods already loaded unchanged;
    2. parses and types the origination file, validates it (Great Expectations);
    3. streams the performance file in chunks, validating each chunk, including referential
       integrity against the origination batch;
    4. inside ONE transaction deletes any previous rows for the period and inserts the new
       ones, so reruns and restated files never duplicate records;
    5. on any failed check rolls back, records the failure, raises an alert and stops.
"""

from __future__ import annotations

import hashlib
import io
import logging
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

import numpy as np
import pandas as pd

from loanlens.alerts import send_alert
from loanlens.config import Settings
from loanlens.quality.suites import ORIGINATION_SENTINELS, PERFORMANCE_SENTINELS
from loanlens.quality.validate import DataQualityError, ValidationOutcome, save_outcome, validate
from loanlens.schemas import (
    LAYOUTS,
    LOAD_METADATA,
    RAW_ORIGINATION,
    RAW_PERFORMANCE,
    classify,
    detect_layout,
)
from loanlens.warehouse import connect, init_schema

log = logging.getLogger(__name__)

@dataclass
class Member:
    container: Path          # the .txt itself, or the .zip holding it
    name: str | None = None  # member name inside the zip

    def open(self) -> IO[bytes]:
        if self.name is None:
            return self.container.open("rb")
        zf = zipfile.ZipFile(self.container)
        fh = zf.open(self.name)
        fh._loanlens_zip = zf  # keep the archive alive as long as the member is open
        return fh


@dataclass
class Source:
    period: str
    members: dict[str, Member] = field(default_factory=dict)  # kind -> member

    @property
    def files(self) -> list[Path]:
        return sorted({m.container for m in self.members.values()})

    @property
    def label(self) -> str:
        return ",".join(m.name or m.container.name for m in self.members.values())

    def fingerprint(self) -> tuple[str, int]:
        digest, size = hashlib.sha256(), 0
        for path in self.files:
            size += path.stat().st_size
            with path.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
        digest.update(self.label.encode())  # a zip holding several periods: one per period
        return digest.hexdigest(), size

    def open_member(self, performance: bool) -> IO[bytes]:
        kind = "performance" if performance else "origination"
        if kind not in self.members:
            raise FileNotFoundError(f"No {kind} file for period {self.period}")
        return self.members[kind].open()


def discover_sources(raw_dir: Path) -> list[Source]:
    found: dict[str, Source] = {}

    def add(period: str, kind: str, member: Member) -> None:
        src = found.setdefault(period, Source(period))
        # prefer a zipped copy over a loose file only if no member was registered yet
        src.members.setdefault(kind, member)

    unrecognised: list[str] = []
    for path in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
        if path.name.startswith("."):
            continue
        if path.suffix.lower() == ".zip":
            try:
                names = zipfile.ZipFile(path).namelist()
            except zipfile.BadZipFile:
                log.warning("Skipping unreadable zip %s", path.name)
                continue
            for name in names:
                if (hit := classify(name)):
                    add(hit[0], hit[1], Member(path, name))
                elif name.lower().endswith(".zip"):
                    unrecognised.append(f"{path.name}/{name} (nested zip - extract it into the raw folder)")
                elif not name.endswith("/"):
                    unrecognised.append(f"{path.name}/{name}")
        elif (hit := classify(path.name)):
            add(hit[0], hit[1], Member(path))
        else:
            unrecognised.append(path.name)
    if unrecognised:
        log.warning("Ignored %d file(s) whose names match no Freddie Mac pattern: %s",
                    len(unrecognised), ", ".join(unrecognised[:10]))
    complete = []
    for period in sorted(found):
        src = found[period]
        if len(src.members) == 2:
            complete.append(src)
        else:
            log.warning("Period %s has only a %s file - skipped", period, next(iter(src.members)))
    return complete


# ---- parsing ------------------------------------------------------------------------------

def _peek_layout(source: Source, kind: str) -> str:
    with source.open_member(performance=kind == "performance") as fh:
        first = io.TextIOWrapper(fh, encoding="utf-8", errors="replace").readline()
    return detect_layout(kind, first)


def _read(fh: IO[bytes], layout, chunk_rows: int | None) -> Iterator[pd.DataFrame]:
    names = [n for n, _ in layout]
    reader = pd.read_csv(
        fh, sep="|", header=None, names=names, dtype=str, keep_default_na=False,
        na_values=[""], chunksize=chunk_rows, engine="c", on_bad_lines="error",
        index_col=False, encoding="utf-8", encoding_errors="replace",
    )
    yield from ([reader] if chunk_rows is None else reader)


def _type(df: pd.DataFrame, layout, raw_layout) -> pd.DataFrame:
    """Cast to warehouse types, add the columns this layout lacks (NULL), and count values
    that were present but failed to parse."""
    parse_errors = np.zeros(len(df), dtype=int)
    out = pd.DataFrame(index=df.index)
    present = {n for n, _ in layout}
    for name, dtype in raw_layout:
        if name not in present:
            if dtype == "INTEGER":
                out[name] = pd.Series(pd.NA, index=df.index, dtype="Int64")
            else:
                out[name] = pd.Series(np.nan if dtype == "DOUBLE" else None, index=df.index,
                                      dtype="float64" if dtype == "DOUBLE" else "object")
            continue
        col = df[name]
        if dtype in ("INTEGER", "DOUBLE") and name != "net_sale_proceeds":
            num = pd.to_numeric(col.str.strip(), errors="coerce")
            parse_errors += (col.notna() & num.isna()).to_numpy()
            out[name] = num.round().astype("Int64") if dtype == "INTEGER" else num
        else:
            out[name] = col.str.strip()
    out["_parse_errors"] = parse_errors
    return out


def _validation_view(df: pd.DataFrame, sentinels: dict[str, list]) -> pd.DataFrame:
    view = df.copy()
    for col, values in sentinels.items():
        view[col] = view[col].astype("float64").where(~view[col].isin(values))
    for col in view.columns:
        if str(view[col].dtype) == "Int64":
            view[col] = view[col].astype("float64")
    return view


def _period_matches(first_payment: pd.Series, period: str) -> pd.Series:
    """First payment should fall 0-15 months after the start of the file's origination period
    (loans close in the period and pay 1-2 months later; annual files span 12 months)."""
    year = int(period[:4])
    start_month = (int(period[5]) - 1) * 3 + 1 if "Q" in period else 1
    span = 3 if "Q" in period else 12
    fp = pd.to_datetime(first_payment, format="%Y%m", errors="coerce")
    lag = (fp.dt.year * 12 + fp.dt.month) - (year * 12 + start_month)
    return lag.between(0, span + 3)


# ---- loading ------------------------------------------------------------------------------

@dataclass
class LoadResult:
    period: str
    status: str
    orig_rows: int = 0
    perf_rows: int = 0
    message: str = ""


def _insert(con, table: str, df: pd.DataFrame, raw_layout, meta: dict) -> None:
    cols = [n for n, _ in raw_layout]
    frame = df[cols].copy()
    for key, value in meta.items():
        frame[key] = value
    con.register("_batch", frame)
    all_cols = cols + [n for n, _ in LOAD_METADATA]
    con.execute(f"INSERT INTO {table} ({', '.join(all_cols)}) SELECT {', '.join(all_cols)} FROM _batch")
    con.unregister("_batch")


def _record(con, outcomes: list[ValidationOutcome], run_id: str, period: str) -> None:
    if not outcomes:
        return
    frame = pd.concat([o.to_frame(run_id, period) for o in outcomes], ignore_index=True)
    con.register("_dq", frame)
    con.execute("""INSERT INTO meta.dq_results SELECT run_id, batch_id, source_period, suite,
                   expectation, "column", success, element_count, unexpected_count,
                   unexpected_percent, examples, validated_at FROM _dq""")
    con.unregister("_dq")


def load_source(settings: Settings, con, source: Source, run_id: str, force: bool = False) -> LoadResult:
    sha, size = source.fingerprint()
    already = con.execute(
        """SELECT count(*) FROM meta.ingest_log
           WHERE source_period = ? AND file_sha256 = ? AND status = 'loaded'""",
        [source.period, sha],
    ).fetchone()[0]
    if already and not force:
        return LoadResult(source.period, "skipped", message="unchanged since last load")

    batch_id = f"{source.period}-{sha[:10]}"
    started = datetime.now()
    meta = {"source_period": source.period, "source_file": source.label,
            "layout_version": None, "batch_id": batch_id, "loaded_at": started}
    outcomes: list[ValidationOutcome] = []
    chunk_rows = settings["ingest"]["chunk_rows"]
    validation_dir = settings.artifacts_dir / "validation"

    def check(outcome: ValidationOutcome) -> None:
        outcomes.append(outcome)
        save_outcome(outcome, validation_dir)
        if not outcome.success:
            raise DataQualityError(outcome)

    con.begin()
    try:
        orig_version = _peek_layout(source, "origination")
        orig_layout = LAYOUTS[("origination", orig_version)]
        with source.open_member(performance=False) as fh:
            orig = _type(next(_read(fh, orig_layout, None)), orig_layout, RAW_ORIGINATION)
        view = _validation_view(orig, ORIGINATION_SENTINELS)
        view["_period_matches_file"] = _period_matches(orig["first_payment_date"], source.period)
        check(validate(view, "freddie_origination", f"{batch_id}-orig"))
        loan_ids = set(orig["loan_sequence_number"])

        con.execute("DELETE FROM raw.origination WHERE source_period = ?", [source.period])
        con.execute("DELETE FROM raw.performance WHERE source_period = ?", [source.period])
        _insert(con, "raw.origination", orig, RAW_ORIGINATION, {**meta, "layout_version": orig_version})

        perf_rows = 0
        perf_version = _peek_layout(source, "performance")
        perf_layout = LAYOUTS[("performance", perf_version)]
        with source.open_member(performance=True) as fh:
            for i, chunk in enumerate(_read(fh, perf_layout, chunk_rows)):
                perf = _type(chunk, perf_layout, RAW_PERFORMANCE)
                view = _validation_view(perf, PERFORMANCE_SENTINELS)
                view["_has_origination"] = perf["loan_sequence_number"].isin(loan_ids)
                check(validate(view, "freddie_performance", f"{batch_id}-perf{i:03d}"))
                _insert(con, "raw.performance", perf, RAW_PERFORMANCE, {**meta, "layout_version": perf_version})
                perf_rows += len(perf)

        con.execute(
            "INSERT INTO meta.ingest_log VALUES (?, ?, ?, ?, ?, ?, 'loaded', ?, ?, ?, ?, ?)",
            [source.period, source.label, sha, size, len(orig), perf_rows, batch_id, run_id,
             started, datetime.now(), "ok"],
        )
        _record(con, outcomes, run_id, source.period)
        con.commit()
        return LoadResult(source.period, "loaded", len(orig), perf_rows)
    except Exception as exc:
        con.rollback()
        message = str(exc)
        con.execute(
            "INSERT INTO meta.ingest_log VALUES (?, ?, ?, ?, NULL, NULL, 'failed', ?, ?, ?, ?, ?)",
            [source.period, source.label, sha, size, batch_id, run_id, started, datetime.now(),
             message[:2000]],
        )
        _record(con, outcomes, run_id, source.period)
        kind = "Data quality check failed" if isinstance(exc, DataQualityError) else "Load failed"
        send_alert(settings, f"{kind}: Freddie Mac {source.period}", message)
        raise


def ingest_freddie(settings: Settings, periods: list[str] | None = None,
                   force: bool = False) -> list[LoadResult]:
    """Load every new or changed source period. Stops at the first failure."""
    sources = discover_sources(settings.raw_freddie_dir)
    if periods:
        sources = [s for s in sources if s.period in set(periods)]
    if not sources:
        raise FileNotFoundError(f"No Freddie Mac files found in {settings.raw_freddie_dir}")
    run_id = uuid.uuid4().hex[:12]
    results = []
    with connect(settings) as con:
        init_schema(con)
        for source in sources:
            result = load_source(settings, con, source, run_id, force=force)
            if result.status == "loaded":
                log.info("Loaded %s: %d loans, %d monthly records", result.period,
                         result.orig_rows, result.perf_rows)
            results.append(result)
    loaded = [r for r in results if r.status == "loaded"]
    log.info("Freddie ingest %s: %d loaded, %d skipped", run_id, len(loaded),
             len(results) - len(loaded))
    return results


def ingest_fred(settings: Settings) -> int:
    """Validate the cached FRED series and (re)load them. Small, so fully replaced each run."""
    from loanlens.ingest.fred import load_observations, series_catalog

    obs = load_observations(settings)
    view = obs.copy()
    rate = view["measure"] != "hpi"
    view["_rate_value"] = view["value"].where(rate)
    view["_hpi_value"] = view["value"].where(~rate)
    run_id = uuid.uuid4().hex[:12]
    outcome = validate(view, "fred_observations", f"fred-{run_id}")
    save_outcome(outcome, settings.artifacts_dir / "validation")
    catalog = pd.DataFrame([vars(s) for s in series_catalog(settings)])
    with connect(settings) as con:
        init_schema(con)
        _record(con, [outcome], run_id, "fred")
        if not outcome.success:
            send_alert(settings, "Data quality check failed: FRED", outcome.describe())
            raise DataQualityError(outcome)
        con.begin()
        con.register("_obs", obs[["series_id", "geo", "measure", "observation_date", "value"]])
        con.register("_cat", catalog)
        con.execute("CREATE OR REPLACE TABLE raw.fred_observations AS SELECT *, now() AS loaded_at FROM _obs")
        con.execute("CREATE OR REPLACE TABLE raw.fred_series AS SELECT * FROM _cat")
        con.commit()
    log.info("FRED: loaded %d observations across %d series", len(obs), len(catalog))
    return len(obs)
