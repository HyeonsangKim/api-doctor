"""고정 검증기 (FR-006, FR-012, AC-09)."""

from __future__ import annotations

import json

import pytest

from api_doctor.registry.loader import load_registry
from api_doctor.verify.verifier import CheckKind, Outcome, load_verifier


@pytest.fixture
def setup():
    dataset = load_registry()["seoul_library"]
    verifier = load_verifier(dataset.contract, dataset.expected_path())
    expected = json.loads(dataset.expected_path().read_text(encoding="utf-8"))
    return dataset, verifier, expected


def _verify(verifier, records, expected, error=None):
    return verifier.verify(
        records=records, execution_error=error,
        candidate_hash="sha256:test", snapshot_hash=expected["snapshot_hash"],
    )


def test_complete_result_passes(setup) -> None:
    _, verifier, expected = setup
    verdict = _verify(verifier, list(expected["records"]), expected)
    assert verdict.passed
    assert all(r.outcome is Outcome.PASS for r in verdict.results)


def test_missing_records_fail_completeness_only(setup) -> None:
    """제품의 핵심 명제: 실행 성공과 데이터 복구는 다르다."""
    _, verifier, expected = setup
    verdict = _verify(verifier, list(expected["records"])[:10], expected)
    assert not verdict.passed
    by_kind = {r.kind: r for r in verdict.results}
    assert by_kind[CheckKind.EXECUTION].outcome is Outcome.PASS
    assert by_kind[CheckKind.FIELDS].outcome is Outcome.PASS
    assert by_kind[CheckKind.VALUES].outcome is Outcome.PASS
    assert by_kind[CheckKind.COMPLETENESS].outcome is Outcome.FAIL
    assert by_kind[CheckKind.COMPLETENESS].metrics["missing_total"] == 2


def test_empty_result_is_inconclusive_not_pass(setup) -> None:
    """0건일 때 필드·값 검사를 통과로 표기하면 보고서가 거짓말을 한다."""
    _, verifier, expected = setup
    verdict = _verify(verifier, [], expected)
    assert not verdict.passed
    by_kind = {r.kind: r for r in verdict.results}
    assert by_kind[CheckKind.FIELDS].outcome is Outcome.INCONCLUSIVE
    assert by_kind[CheckKind.VALUES].outcome is Outcome.INCONCLUSIVE
    assert by_kind[CheckKind.COMPLETENESS].outcome is Outcome.FAIL


def test_fixed_output_cannot_pass(setup) -> None:
    """AC-09: 고정값·조작된 배열로 검증을 통과할 수 없다."""
    _, verifier, expected = setup
    forged = [{"LBRRY_SEQ_NO": "1", "LBRRY_NAME": "가짜", "ADRES": "", "TEL_NO": ""}] * 12
    verdict = _verify(verifier, forged, expected)
    assert not verdict.passed
    completeness = next(r for r in verdict.results if r.kind is CheckKind.COMPLETENESS)
    assert completeness.metrics["duplicate_ids"]


def test_dropped_field_fails_fields_check(setup) -> None:
    _, verifier, expected = setup
    stripped = [{k: v for k, v in r.items() if k != "ADRES"} for r in expected["records"]]
    verdict = _verify(verifier, stripped, expected)
    assert not verdict.passed
    fields = next(r for r in verdict.results if r.kind is CheckKind.FIELDS)
    assert fields.metrics["missing_field_counts"]["ADRES"] == 12


def test_value_rule_violation_is_caught(setup) -> None:
    """스키마는 맞지만 값 의미가 깨진 경우."""
    _, verifier, expected = setup
    records = [dict(r) for r in expected["records"]]
    records[0]["LBRRY_SEQ_NO"] = "일번"     # 숫자가 아니다
    records[1]["LBRRY_NAME"] = "   "        # 공백뿐이다
    verdict = _verify(verifier, records, expected)
    assert not verdict.passed
    values = next(r for r in verdict.results if r.kind is CheckKind.VALUES)
    assert set(values.metrics["violations"]) == {"seq_no_is_digits", "name_not_blank"}


def test_snapshot_mismatch_is_inconclusive_not_failure(setup) -> None:
    """AC-14: 기대값과 다른 스냅샷이면 판정할 수 없다."""
    _, verifier, expected = setup
    verdict = verifier.verify(
        records=list(expected["records"]), execution_error=None,
        candidate_hash="sha256:test", snapshot_hash="sha256:다른스냅샷",
    )
    assert not verdict.passed
    assert verdict.results[0].outcome is Outcome.INCONCLUSIVE


def test_execution_error_fails_execution_check(setup) -> None:
    _, verifier, expected = setup
    verdict = _verify(verifier, None, expected, error="KeyError: 'row'")
    assert not verdict.passed
    assert verdict.results[0].outcome is Outcome.FAIL


def test_all_four_checks_always_run(setup) -> None:
    """감사자가 고른 probe 는 필수 검사 수를 줄일 수 없다 (PRD §3.2 규칙 5)."""
    _, verifier, expected = setup
    for records in ([], list(expected["records"]), list(expected["records"])[:3]):
        verdict = _verify(verifier, records, expected)
        assert {r.kind for r in verdict.results} == set(CheckKind)
