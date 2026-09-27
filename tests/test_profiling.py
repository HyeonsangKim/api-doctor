"""역할별 사용량 프로파일 (FR-018, AC-16)."""

from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

from api_doctor.model.gateway import ScriptedBackend
from api_doctor.profiling.nat import (
    SOURCE_LEDGER, SOURCE_NAT, collect_spans, per_role, reconcile, write_all,
)
from api_doctor.runtime.recover import recover
from api_doctor.runtime.store import RunStore

from _docker import requires_docker
from _harness import deep_happy_script

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SVG_NS = "{http://www.w3.org/2000/svg}"


def _recover(tmp_path):
    return recover(
        dataset_id="seoul_library",
        code_path=EXAMPLES / "connector_broken.py",
        store=RunStore(tmp_path / "runs"),
        model_backend=ScriptedBackend(
            responses=deep_happy_script(),
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        scripted=True,
    )


@requires_docker
def test_three_artifacts_are_produced(tmp_path) -> None:
    """FR-018 산출물 3종."""
    result = _recover(tmp_path)
    directory = result.baseline.paths.run_dir / "profiling"
    assert (directory / "standardized_data_all.csv").is_file()
    assert (directory / "workflow_profiling_report.txt").is_file()
    assert (directory / "gantt_chart.svg").is_file()


@requires_docker
def test_every_role_appears_with_calls(tmp_path) -> None:
    """main 과 네 역할 각각의 호출·토큰·지연이 산출된다."""
    result = _recover(tmp_path)
    text = (result.baseline.paths.run_dir / "profiling"
            / "workflow_profiling_report.txt").read_text(encoding="utf-8")
    for agent_id in ("main", "spec_researcher", "runtime_diagnostician",
                     "repair_engineer", "data_auditor"):
        assert agent_id in text, f"{agent_id} 가 프로파일에 없다"


@requires_docker
def test_role_totals_match_the_ledger(tmp_path) -> None:
    """AC-16: 역할별 호출 수 합계가 gateway 원장과 일치한다."""
    result = _recover(tmp_path)
    text = (result.baseline.paths.run_dir / "profiling"
            / "workflow_profiling_report.txt").read_text(encoding="utf-8")
    assert "일치합니다" in text


@requires_docker
def test_csv_rows_cover_model_and_tool_spans(tmp_path) -> None:
    result = _recover(tmp_path)
    path = result.baseline.paths.run_dir / "profiling" / "standardized_data_all.csv"
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    kinds = {row["kind"] for row in rows}
    assert kinds == {"model", "tool"}
    assert all(row["source"] == SOURCE_LEDGER for row in rows)
    assert {row["agent_id"] for row in rows} <= {
        "main", "spec_researcher", "runtime_diagnostician",
        "repair_engineer", "data_auditor",
    }


@requires_docker
def test_gantt_has_one_lane_per_active_role(tmp_path) -> None:
    result = _recover(tmp_path)
    path = result.baseline.paths.run_dir / "profiling" / "gantt_chart.svg"
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    labels = {t.text for t in root.findall(f".//{SVG_NS}text") if t.text}
    for agent_id in ("main", "spec_researcher", "runtime_diagnostician",
                     "repair_engineer", "data_auditor"):
        assert agent_id in labels
    assert root.findall(f".//{SVG_NS}rect"), "막대가 하나도 없다"


@requires_docker
def test_source_is_labeled_as_ledger_not_nat(tmp_path) -> None:
    """출처를 속이지 않는다.

    NAT 프로파일러를 돌리지 않았으면 NAT 산출물이라고 적지 않는다.
    """
    result = _recover(tmp_path)
    text = (result.baseline.paths.run_dir / "profiling"
            / "workflow_profiling_report.txt").read_text(encoding="utf-8")
    assert f"출처: {SOURCE_LEDGER}" in text
    assert SOURCE_NAT not in text


def test_reconcile_reports_differences_and_defers_to_ledger() -> None:
    """AC-16: 프로파일러와 다르면 차이를 적고 예산 판단은 원장을 따른다."""
    from api_doctor.model.gateway import ModelCall

    calls = [
        ModelCall("main", 100, 20, 5, "actual"),
        ModelCall("main", 100, 20, 5, "actual"),
        ModelCall("data_auditor", 100, 20, 5, "actual"),
    ]
    assert reconcile(calls, {"main": 2, "data_auditor": 1})["matches"]

    mismatch = reconcile(calls, {"main": 2})
    assert not mismatch["matches"]
    assert mismatch["differences"]["data_auditor"] == {"ledger": 1, "profiler": 0}
    assert mismatch["authority"] == "gateway_ledger"


def test_spans_are_ordered_by_start_time() -> None:
    from api_doctor.model.gateway import ModelCall

    calls = [
        ModelCall("main", 1, 1, 5, "actual", started_ms=200, ended_ms=260),
        ModelCall("data_auditor", 1, 1, 5, "actual", started_ms=10, ended_ms=90),
    ]
    tools = [{"agent_id": "data_auditor", "tool": "run_probe", "allowed": True,
              "started_ms": 95, "ended_ms": 140}]
    spans = collect_spans(calls, tools)
    assert [s.started_ms for s in spans] == [10, 95, 200]
    assert spans[1].kind == "tool"


def test_per_role_separates_estimated_tokens() -> None:
    from api_doctor.model.gateway import ModelCall

    rows = per_role([
        ModelCall("main", 100, 20, 5, "actual"),
        ModelCall("main", None, None, 5, "estimated"),
    ])
    assert rows["main"]["calls"] == 2
    assert rows["main"]["estimated_calls"] == 1


def test_empty_run_writes_placeholder_gantt(tmp_path) -> None:
    paths = write_all(tmp_path, model_calls=[], tool_log=[], ledger_usage={})
    assert paths["gantt"].is_file()
    assert "구간 기록이 없습니다" in paths["gantt"].read_text(encoding="utf-8")
