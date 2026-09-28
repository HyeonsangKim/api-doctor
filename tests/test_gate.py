"""종료 게이트 (FR-012, AC-04, AC-09, AC-13)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from api_doctor.data.broker import Snapshot
from api_doctor.registry.loader import load_registry
from api_doctor.runtime.budget import BudgetLedger, Limits
from api_doctor.runtime.events import EventStore, RunStatus
from api_doctor.runtime.evidence import EvidenceStore
from api_doctor.runtime.gate import GateRejection, audit_completeness, evaluate
from api_doctor.runtime.session import AuditRecord, RunSession
from api_doctor.runtime.store import RunStore
from api_doctor.sandbox.base import SandboxLimits
from api_doctor.sandbox.selftest import select_backend
from api_doctor.verify.verifier import load_verifier

from _docker import requires_docker

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def session(tmp_path):
    dataset = load_registry()["seoul_library"]
    snapshot = Snapshot.load(
        dataset.snapshot_path("seoul_library_scope1_5"), dataset.dataset_id
    )
    store = RunStore(tmp_path / "runs")
    run_id, paths = store.create()
    return RunSession(
        run_id=run_id, paths=paths, events=EventStore(paths.run_dir),
        ledger=BudgetLedger(limits=Limits(sandbox_runs=30, broker_calls=300)),
        evidence=EvidenceStore(paths.run_dir), dataset=dataset, snapshot=snapshot,
        backend=select_backend(SandboxLimits()).backend,
        verifier=load_verifier(dataset.contract, dataset.expected_path()),
    )


def _load(session: RunSession, name: str) -> str:
    source = (EXAMPLES / f"connector_{name}.py").read_text(encoding="utf-8")
    session.register_candidate(source=source, version=0, base_hash=None, author="operator")
    return session.current_hash


def _final_verify(session: RunSession, candidate_hash: str):
    records, error, *_ = session.execute_candidate(
        candidate_hash, session.dataset.contract.query, is_finish=True
    )
    return session.verifier.verify(
        records=records, execution_error=error,
        candidate_hash=candidate_hash, snapshot_hash=session.snapshot.snapshot_hash,
    )


def _record_audit(session: RunSession, candidate_hash: str, conclusion: str) -> None:
    for requirement in session.dataset.contract.audit_requirements:
        session.audit_records.append(
            AuditRecord(
                candidate_hash=candidate_hash, risk_id=requirement.risk_id,
                hypothesis="경계에서 레코드가 빠질 수 있다", invariant="partition_invariance",
                evidence_ids=("ev_obs_001",), probe_result_ids=("pr_x",),
                conclusion=conclusion,
            )
        )


@requires_docker
def test_healthy_candidate_passes_every_gate(session) -> None:
    candidate_hash = _load(session, "healthy")
    session.run_probe("page_partition", candidate_hash)
    session.run_probe("field_presence", candidate_hash)
    _record_audit(session, candidate_hash, "no_issue")

    decision = evaluate(
        session, candidate_hash=candidate_hash,
        final_verdict=_final_verify(session, candidate_hash),
    )
    assert decision.status is RunStatus.VERIFIED_REPAIRED
    assert all(decision.checks.values())


@requires_docker
def test_missing_audit_is_returned_to_main(session) -> None:
    """AC-04: 감사 없이는 verified 가 될 수 없다."""
    candidate_hash = _load(session, "healthy")
    decision = evaluate(
        session, candidate_hash=candidate_hash,
        final_verdict=_final_verify(session, candidate_hash),
    )
    assert not decision.terminal
    assert decision.rejection is GateRejection.MISSING_AUDIT
    assert "baseline 과 다른 probe" in decision.detail


@requires_docker
def test_baseline_probe_alone_does_not_complete_audit(session) -> None:
    """baseline 만 돌려서는 감사가 끝나지 않는다."""
    candidate_hash = _load(session, "healthy")
    session.run_probe("baseline_full_scope", candidate_hash)
    _record_audit(session, candidate_hash, "no_issue")
    completeness = audit_completeness(session, candidate_hash)
    assert not completeness.complete
    assert not completeness.ran_non_baseline_probe


@requires_docker
def test_vacuous_probe_does_not_cover_risk(session) -> None:
    """0건 후보의 공허한 분할 성립을 감사 근거로 세면 안 된다."""
    candidate_hash = _load(session, "broken")
    result = session.run_probe("page_partition", candidate_hash)
    assert str(result.outcome) == "vacuous"
    completeness = audit_completeness(session, candidate_hash)
    assert "pagination_boundary" not in completeness.covered_risks


@requires_docker
def test_auditor_approval_alone_cannot_pass(session) -> None:
    """AC-04: 감사자의 찬성만으로 verified 상태를 만들 수 없다."""
    candidate_hash = _load(session, "healthy")
    _record_audit(session, candidate_hash, "no_issue")   # probe 는 하나도 안 돌렸다
    decision = evaluate(
        session, candidate_hash=candidate_hash,
        final_verdict=_final_verify(session, candidate_hash),
    )
    assert decision.rejection is GateRejection.MISSING_AUDIT


@requires_docker
def test_unresolved_finding_blocks_success(session) -> None:
    """해소되지 않은 계약 관련 우려는 보류다. main 동의로 덮지 못한다."""
    candidate_hash = _load(session, "healthy")
    session.run_probe("page_partition", candidate_hash)
    session.run_probe("field_presence", candidate_hash)
    _record_audit(session, candidate_hash, "issue")

    decision = evaluate(
        session, candidate_hash=candidate_hash,
        final_verdict=_final_verify(session, candidate_hash),
    )
    assert decision.status is RunStatus.VERIFICATION_INCONCLUSIVE
    assert decision.rejection is GateRejection.UNRESOLVED_FINDING


@requires_docker
def test_failed_verification_never_reaches_audit_stage(session) -> None:
    candidate_hash = _load(session, "partial")
    decision = evaluate(
        session, candidate_hash=candidate_hash,
        final_verdict=_final_verify(session, candidate_hash),
    )
    assert decision.status is RunStatus.VERIFICATION_FAILED
    assert decision.checks["fixed_check_passed"] is False
    assert "audit_complete" not in decision.checks


@requires_docker
def test_verdict_from_other_candidate_is_stale(session) -> None:
    """AC-13: 이전 hash 의 결과를 최신 검증으로 재사용하지 않는다."""
    first = _load(session, "healthy")
    verdict = _final_verify(session, first)
    session.register_candidate(
        source=(EXAMPLES / "connector_partial.py").read_text(encoding="utf-8"),
        version=1, base_hash=first, author="repair_engineer",
    )
    decision = evaluate(
        session, candidate_hash=session.current_hash, final_verdict=verdict
    )
    assert decision.rejection is GateRejection.STALE


@requires_docker
def test_audit_expires_when_patch_changes(session) -> None:
    """패치가 바뀌면 이전 감사는 만료된다."""
    first = _load(session, "healthy")
    session.run_probe("page_partition", first)     # pagination_boundary
    session.run_probe("field_presence", first)     # mapping_preservation
    _record_audit(session, first, "no_issue")
    assert audit_completeness(session, first).complete

    session.register_candidate(
        source=(EXAMPLES / "connector_healthy.py").read_text(encoding="utf-8") + "\n# v2\n",
        version=1, base_hash=first, author="repair_engineer",
    )
    assert not audit_completeness(session, session.current_hash).complete


@requires_docker
def test_forced_halt_is_not_overridden_by_passing_checks(session) -> None:
    """AC-08: 강제 중단 후에는 완료된 검사가 있어도 verified 로 승격하지 않는다."""
    candidate_hash = _load(session, "healthy")
    session.run_probe("page_partition", candidate_hash)
    session.run_probe("field_presence", candidate_hash)
    _record_audit(session, candidate_hash, "no_issue")
    verdict = _final_verify(session, candidate_hash)
    assert verdict.passed

    session.forced_halt = str(RunStatus.CANCELLED)
    decision = evaluate(
        session, candidate_hash=candidate_hash, final_verdict=verdict
    )
    assert decision.status is RunStatus.CANCELLED
    assert decision.checks["no_forced_halt"] is False


def test_gate_takes_no_model_output_as_input() -> None:
    """모델의 주장은 판정 함수의 인자가 아니다 (AC-09)."""
    import inspect

    signature = inspect.signature(evaluate)
    assert set(signature.parameters) == {"session", "candidate_hash", "final_verdict"}


@requires_docker
def test_final_verification_is_not_starved_by_earlier_probes(session) -> None:
    """종료 검증이 앞선 probe 때문에 굶으면 안 된다.

    평가에서 실제로 일어났다 — 같은 후보가 세 번 정상 실행된 뒤
    가장 중요한 종료 검증만 실패했다. 원인은 동결분 읽기를 공식 API 호출
    상한에 넣은 것이었다. PRD §4.1: "최종 검증은 이미 동결된 데이터만 사용해
    새 공식 API 호출을 요구하지 않는다."
    """
    candidate_hash = _load(session, "healthy")

    # 상한(20)을 훌쩍 넘는 probe 를 먼저 돌린다.
    for _ in range(4):
        session.run_probe("page_partition", candidate_hash)
        session.run_probe("field_presence", candidate_hash)
    assert session.ledger.usage()["broker_calls"] > 20, "상한을 넘겨야 의미가 있다"

    records, error, *_ = session.execute_candidate(
        candidate_hash, session.dataset.contract.query, is_finish=True
    )
    assert error is None, f"종료 검증이 굶었다: {error}"
    assert records and len(records) == 5


def test_fixture_reads_do_not_consume_the_provider_quota() -> None:
    """fixture 는 동결분만 읽으므로 공급자 호출이 발생하지 않는다."""
    from api_doctor.runtime.budget import BudgetLedger

    ledger = BudgetLedger()
    ledger.record_broker_calls(50, enforce=False)
    assert ledger.broker_calls == 50, "관측은 그대로 기록한다"
    assert ledger.can_call_broker(), "상한을 적용하지 않는다"

    live = BudgetLedger()
    live.record_broker_calls(50, enforce=True)
    assert not live.can_call_broker(), "live 에서는 상한이 적용된다"
