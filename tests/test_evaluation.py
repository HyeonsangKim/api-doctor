"""평가 세트 (FR-017, PRD §7.1~7.2)."""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.evaluation.runner import load_cases, run_detection
from api_doctor.registry.loader import load_registry

from _docker import requires_docker

EVAL_ROOT = Path(__file__).resolve().parents[1] / "eval"


def _hidden_fixture():
    """비공개 검사에 필요한 데이터셋·스냅샷·백엔드."""
    from api_doctor.data.broker import Snapshot
    from api_doctor.sandbox.base import SandboxLimits
    from api_doctor.sandbox.selftest import select_backend

    dataset = load_registry()["seoul_library"]
    snapshot = Snapshot.load(
        dataset.snapshot_path(dataset.default_snapshot or ""), "seoul_library"
    )
    return dataset, snapshot, select_backend(SandboxLimits()).backend


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


# ------------------------------------------------------- 비공개 검사 (PRD §7.1 b)


def test_hidden_variants_use_ranges_the_team_never_optimizes_for() -> None:
    """팀은 계약의 주 query 로 작업한다. 비공개 세트는 다른 범위를 쓴다."""
    from api_doctor.evaluation.hidden import load_variants
    from api_doctor.registry.loader import load_registry

    contract_query = load_registry()["seoul_library"].contract.query
    variants = load_variants(EVAL_ROOT)
    assert variants
    for variant in variants:
        assert variant.query != contract_query, (
            f"{variant.name} 이 계약의 주 query 와 같다 — 독립 검사가 아니다"
        )


def test_hidden_expectations_are_not_readable_by_agents() -> None:
    """평가용 기대값은 에이전트 도구가 닿는 경로 밖에 있어야 한다."""
    hidden = EVAL_ROOT / "hidden.json"
    assert hidden.is_file()
    assert hidden.stat().st_mode & 0o077 == 0, "비공개 파일이 0600 이 아니다"

    # 레지스트리 밖에 있으므로 search_spec · read_evidence 로 닿을 수 없다
    registry_root = load_registry()["seoul_library"].root
    assert registry_root not in hidden.parents


@requires_docker
def test_hidden_check_catches_partial_repair(tmp_path) -> None:
    """주 query 에서 통과하는 변형이 있어도 다른 범위에서 걸린다."""
    from api_doctor.data.broker import Snapshot
    from api_doctor.evaluation.hidden import check, load_variants
    from api_doctor.registry.loader import load_registry
    from api_doctor.sandbox.base import SandboxLimits
    from api_doctor.sandbox.selftest import select_backend

    dataset = load_registry()["seoul_library"]
    snapshot = Snapshot.load(
        dataset.snapshot_path(dataset.default_snapshot), dataset.dataset_id
    )
    backend = select_backend(SandboxLimits()).backend
    variants = load_variants(EVAL_ROOT)
    examples = EVAL_ROOT.parent / "examples"

    healthy = check(
        candidate=examples / "connector_healthy.py", dataset=dataset,
        snapshot=snapshot, backend=backend, variants=variants,
    )
    assert healthy.passed, "정상 코드가 비공개 검사에서 떨어졌다"

    partial = check(
        candidate=examples / "connector_partial.py", dataset=dataset,
        snapshot=snapshot, backend=backend, variants=variants,
    )
    assert not partial.passed, "절반만 고친 코드를 비공개 검사가 통과시켰다"
    assert any(r.passed for r in partial.results), (
        "일부 변형은 통과해야 이 검사의 가치가 드러난다"
    )


def test_recovery_only_targets_repairable_kinds() -> None:
    """정상 코드와 경계 사례는 복구 대상이 아니다."""
    from api_doctor.evaluation.runner import RECOVERABLE_KINDS

    assert "healthy" not in RECOVERABLE_KINDS
    assert "boundary" not in RECOVERABLE_KINDS
    assert RECOVERABLE_KINDS == {"format", "mapping", "range", "composite"}


def test_false_success_requires_both_claim_and_hidden_failure() -> None:
    """거짓 성공의 정의: 시스템이 성공을 주장했는데 비공개 검사가 틀렸다고 한 것."""
    from api_doctor.evaluation.runner import RecoveryResultRow
    from api_doctor.runtime.events import RunStatus

    def row(status, hidden):
        return RecoveryResultRow(
            name="x", kind="format", split="locked", status=status,
            claimed_success=status.is_success, hidden_passed=hidden,
            model_calls=0, tokens=0, provider_failures=0, duration_ms=0,
        )

    assert row(RunStatus.VERIFIED_REPAIRED, False).false_success
    assert not row(RunStatus.VERIFIED_REPAIRED, True).false_success
    assert row(RunStatus.VERIFIED_REPAIRED, True).recovered
    # 성공을 주장하지 않았으면 비공개 검사가 실패해도 거짓 성공이 아니다
    assert not row(RunStatus.VERIFICATION_INCONCLUSIVE, False).false_success
    assert not row(RunStatus.VERIFICATION_INCONCLUSIVE, False).recovered


# ------------------------------------------- 비공개 검사가 실제로 무엇을 잡나


@requires_docker
def test_hidden_check_catches_every_defect_kind_in_the_dev_set() -> None:
    """비공개 검사가 각 결함 종류를 실제로 잡아내야 한다.

    이걸 확인하지 않아 한동안 mapping 결함이 비공개 검사를 그냥 지나갔다.
    건수와 식별키만 보고 있었고, 필드를 통째로 떨어뜨린 후보가 통과했다.
    그 결과를 "실제로는 고쳐졌다" 로 잘못 읽었다.
    """
    from api_doctor.evaluation.hidden import check, load_variants

    root = EVAL_ROOT
    cases = load_cases(root)
    variants = load_variants(root)
    dataset, snapshot, backend = _hidden_fixture()

    defective = [c for c in cases if c["kind"] not in ("healthy", "boundary")]
    assert defective, "결함 사례가 없다"

    passed_anyway = []
    for case in defective:
        verdict = check(
            candidate=root / "cases" / f"{case['name']}.py",
            dataset=dataset, snapshot=snapshot, backend=backend, variants=variants,
        )
        if verdict.passed:
            passed_anyway.append(case["name"])

    assert not passed_anyway, (
        f"비공개 검사를 그냥 지나간 결함: {passed_anyway}. "
        "검사가 제품의 고정 검증보다 약하면 평가가 거짓 신호를 낸다."
    )


@requires_docker
def test_hidden_check_passes_healthy_candidates() -> None:
    """정상 후보를 결함으로 잡으면 거짓 실패가 된다."""
    from api_doctor.evaluation.hidden import check, load_variants

    root = EVAL_ROOT
    cases = load_cases(root)
    variants = load_variants(root)
    dataset, snapshot, backend = _hidden_fixture()

    for case in (c for c in cases if c["kind"] == "healthy"):
        verdict = check(
            candidate=root / "cases" / f"{case['name']}.py",
            dataset=dataset, snapshot=snapshot, backend=backend, variants=variants,
        )
        assert verdict.passed, (
            f"{case['name']} 를 잘못 잡았다: "
            f"{[r.to_json() for r in verdict.results if not r.passed]}"
        )
