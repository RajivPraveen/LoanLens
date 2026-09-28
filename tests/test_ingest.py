"""Incremental loading, idempotency and data-quality gates."""

from __future__ import annotations

import json
import zipfile

import pytest

from loanlens.ingest.freddie import classify, ingest_fred, ingest_freddie
from loanlens.quality.validate import DataQualityError
from loanlens.warehouse import query


def _generate(settings, quarters):
    from loanlens.synthetic.generator import generate

    return generate(settings, quarters=quarters, workers=1)


def _rewrite_member(path, performance: bool, transform):
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n).decode() for n in zf.namelist()}
    kind = "performance" if performance else "origination"
    name = next(n for n in members if (hit := classify(n)) and hit[1] == kind)
    members[name] = transform(members[name])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for n, text in members.items():
            zf.writestr(n, text)


def test_incremental_and_idempotent(fresh_settings):
    s = fresh_settings
    _generate(s, ["2005Q1", "2005Q2"])
    ingest_fred(s)
    first = ingest_freddie(s)
    assert [r.status for r in first] == ["loaded", "loaded"]
    counts = query(s, "select count(*) as n from raw.performance").iloc[0, 0]

    # Rerun: unchanged files are skipped and nothing is duplicated.
    second = ingest_freddie(s)
    assert [r.status for r in second] == ["skipped", "skipped"]
    assert query(s, "select count(*) from raw.performance").iloc[0, 0] == counts

    # A new quarter arrives: only it is processed.
    _generate(s, ["2005Q3"])
    third = ingest_freddie(s)
    assert [r.status for r in third] == ["skipped", "skipped", "loaded"]

    # A restated file replaces its period instead of appending to it.
    path = s.raw_freddie_dir / "historical_data_2005Q1.zip"
    _rewrite_member(path, True, lambda t: "\n".join(t.splitlines()[:-50]) + "\n")
    fourth = ingest_freddie(s)
    assert [r.status for r in fourth][0] == "loaded"
    dupes = query(s, """select count(*) from (select loan_sequence_number, monthly_reporting_period, count(*) c
                        from raw.performance group by all having c > 1)""").iloc[0, 0]
    assert dupes == 0
    assert query(s, "select count(distinct batch_id) from raw.origination where source_period = '2005Q1'").iloc[0, 0] == 1


def test_impossible_value_stops_the_load_and_alerts(fresh_settings):
    s = fresh_settings
    _generate(s, ["2006Q1"])
    ingest_fred(s)
    path = s.raw_freddie_dir / "historical_data_2006Q1.zip"

    def corrupt(text):
        lines = text.splitlines()
        fields = lines[0].split("|")
        fields[0] = "900"  # credit score above 850
        lines[0] = "|".join(fields)
        return "\n".join(lines) + "\n"

    _rewrite_member(path, False, corrupt)
    with pytest.raises(DataQualityError) as err:
        ingest_freddie(s)
    assert "credit_score" in str(err.value)
    assert query(s, "select count(*) from raw.origination").iloc[0, 0] == 0          # rolled back
    log = query(s, "select status from meta.ingest_log")
    assert list(log["status"]) == ["failed"]
    alerts = list(s.alerts_dir.glob("alert_*.json"))
    assert alerts and "2006Q1" in json.loads(alerts[0].read_text())["title"]


def test_orphan_performance_record_fails_referential_check(fresh_settings):
    s = fresh_settings
    _generate(s, ["2007Q1"])
    ingest_fred(s)
    path = s.raw_freddie_dir / "historical_data_2007Q1.zip"
    _rewrite_member(path, True, lambda t: t + t.splitlines()[0].replace("F07Q1", "F07Q9", 1) + "\n")
    with pytest.raises(DataQualityError) as err:
        ingest_freddie(s)
    assert "_has_origination" in str(err.value)
