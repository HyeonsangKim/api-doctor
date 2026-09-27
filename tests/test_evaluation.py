"""평가 세트 (FR-017, PRD §7.1~7.2)."""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.evaluation.runner import load_cases, run_detection

from _docker import requires_docker

EVAL_ROOT = Path(__file__).resolve().parents[1] / "eval"


def test_every_case_file_exists() -> None:
    for case in load_cases(EVAL_ROOT):
        path = EVAL_ROOT / "cases" / f"{case['name']}.py"
        assert path.is_file(), f"사례 파일이 없습니다: {case['name']}"
        assert "def fetch_records" in path.read_text(encoding="utf-8")


def test_cases_cover_every_supported_defect_kind() -> None:
    kinds = {c["kind"] for c in load_cases(EVAL_ROOT)}
    assert kinds == {"format", "mapping", "range", "composite", "healthy", "boundary"}


def test_dev_and_locked_sets_are_separated() -> None:
    """PRD §7.1: 잠금 평가 결과를 보고 설계를 바꾸면 개발 세트로 전환한다."""
    cases = load_cases(EVAL_ROOT)
    splits = {c["split"] for c in cases}
    assert splits == {"dev", "locked"}
    assert sum(c["split"] == "locked" for c in cases) >= 8


@requires_docker
def test_detection_has_no_false_positives_or_false_successes(tmp_path) -> None:
    """PRD §7.2 의 핵심 두 지표.

    정상 코드를 결함으로 오인하지 않고, 결함 코드를 통과시키지 않는다.
    """
    report = run_detection(cases_root=EVAL_ROOT, store_root=tmp_path / "runs")
    assert report.false_positives == 0, "정상 코드를 훼손했다"
    assert report.false_successes == 0, "결함 코드를 통과시켰다"


@requires_docker
def test_every_case_is_judged_correctly(tmp_path) -> None:
    report = run_detection(cases_root=EVAL_ROOT, store_root=tmp_path / "runs")
    wrong = [r.name for r in report.results if not r.detected]
    assert not wrong, f"오판정: {wrong}"


@requires_docker
def test_detection_uses_no_model_calls(tmp_path) -> None:
    """검출 평가는 모델을 호출하지 않는다."""
    report = run_detection(cases_root=EVAL_ROOT, store_root=tmp_path / "runs")
    assert all(r.model_calls == 0 for r in report.results)
    assert not report.recovery_attempted


@requires_docker
def test_boundary_cases_record_policy_denials(tmp_path) -> None:
    """AC-15: 경계를 건드린 시도는 차단 기록이 남아야 한다."""
    report = run_detection(
        cases_root=EVAL_ROOT, store_root=tmp_path / "runs", split="dev"
    )
    boundary = [r for r in report.results if r.kind == "boundary"]
    assert boundary
    recorded = [r for r in boundary if r.policy_denials > 0]
    assert recorded, "경계 사례에서 차단 기록을 하나도 얻지 못했다"


@requires_docker
def test_unsupported_request_is_not_a_policy_block(tmp_path) -> None:
    """동결 자료에 없는 범위 요청은 코드 결함이지 정책 위반이 아니다.

    둘을 같이 다루면 고칠 수 있는 버그가 policy_blocked 로 끝난다.
    """
    report = run_detection(cases_root=EVAL_ROOT, store_root=tmp_path / "runs")
    case = next(r for r in report.results if r.name == "range_total_count_terminator")
    assert case.baseline_passed is False
    assert case.policy_denials == 0, "미지원 요청이 정책 위반으로 분류됐다"


@requires_docker
def test_report_serializes(tmp_path) -> None:
    report = run_detection(
        cases_root=EVAL_ROOT, store_root=tmp_path / "runs", split="dev"
    )
    payload = json.loads(json.dumps(report.to_json(), ensure_ascii=False))
    assert payload["summary"]["total"] == len(report.results)
    assert "false_positives" in payload["summary"]
