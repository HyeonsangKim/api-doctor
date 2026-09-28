"""공통 예산 원장과 취소 (FR-003, AC-08)."""

from __future__ import annotations

import pytest

from api_doctor.runtime.budget import (
    AGENT_IDS, BudgetExceeded, BudgetLedger, CancelToken, DenyReason, Limits,
)


def _spend(ledger: BudgetLedger, agent_id: str, tokens: int = 10_048) -> None:
    reservation = ledger.reserve_model_call(agent_id, 8000)
    ledger.settle_model_call(reservation, usage_tokens=tokens)


def test_token_ceiling_binds_before_call_ceiling() -> None:
    """PRD 의 세 상한은 동시에 도달할 수 없다. 원장이 그 사실을 드러내야 한다."""
    ledger = BudgetLedger()
    calls = 0
    while True:
        agent = next((a for a in AGENT_IDS if ledger.can_spend_model_call(a)), None)
        if agent is None:
            break
        _spend(ledger, agent)
        calls += 1
    assert calls < ledger.limits.model_calls, "24회는 도달하지 않는 상한이다"
    assert ledger.can_spend_model_call("main").reason is DenyReason.TOKENS_EXHAUSTED


def test_role_budget_is_not_transferable() -> None:
    ledger = BudgetLedger()
    for _ in range(ledger.limits.role_model_calls["spec_researcher"]):
        _spend(ledger, "spec_researcher", tokens=100)
    verdict = ledger.can_spend_model_call("spec_researcher")
    assert verdict.reason is DenyReason.ROLE_CALLS_EXHAUSTED
    assert ledger.can_spend_model_call("data_auditor"), "다른 역할은 자기 몫이 남아 있다"


def test_finishing_a_delegation_does_not_reset_budget() -> None:
    ledger = BudgetLedger()
    before = ledger.remaining()["model_calls"]
    ledger.spend_delegation("spec_researcher")
    _spend(ledger, "spec_researcher", tokens=100)
    assert ledger.remaining()["model_calls"] < before


def test_finish_slot_cannot_be_consumed_by_normal_runs() -> None:
    """종료 검증용 실행 1회는 양도 불가다 (PRD §4.1).

    감사 예비분은 이 테스트의 관심사가 아니므로 0 으로 두고 분리해서 본다.
    """
    ledger = BudgetLedger(
        limits=Limits(sandbox_runs=3, sandbox_runs_reserved_for_audit=0)
    )
    ledger.spend_sandbox_run()
    ledger.spend_sandbox_run()
    assert ledger.usable_sandbox_runs == 0
    assert ledger.can_run_sandbox().reason is DenyReason.SANDBOX_EXHAUSTED
    assert ledger.can_run_sandbox(is_finish=True), "종료 검증은 예약분을 쓴다"


def test_auditor_keeps_its_own_candidate_runs() -> None:
    """수리자가 자기 패치를 다시 돌려 보다 감사 몫을 먹으면 안 된다.

    실측(2026-09-28): 고정 검증 4/4 를 통과한 수리본이 있었는데
    repair_engineer 가 패치 제출 뒤 세 번 더 돌려 샌드박스 8/8 을 소진했고,
    감사자는 probe 를 한 번도 못 돌려 MISSING_AUDIT 으로 끝났다.
    """
    limits = Limits(sandbox_runs=8, sandbox_runs_reserved_for_audit=2)
    ledger = BudgetLedger(limits=limits)
    # 종료 검증용 1 + 감사용 2 를 빼면 조사·수리 몫은 5 다.
    for _ in range(5):
        ledger.spend_sandbox_run()

    assert not ledger.can_run_sandbox(agent_id="repair_engineer")
    assert not ledger.can_run_sandbox(agent_id="runtime_diagnostician")
    assert ledger.can_run_sandbox(agent_id="data_auditor"), (
        "감사자가 자기 예비분을 못 쓰면 예약한 의미가 없다"
    )

    ledger.spend_sandbox_run(agent_id="data_auditor")
    ledger.spend_sandbox_run(agent_id="data_auditor")
    assert not ledger.can_run_sandbox(agent_id="data_auditor"), (
        "예비분을 다 쓰면 감사자도 멈춘다"
    )
    assert ledger.can_run_sandbox(is_finish=True), "종료 검증 몫은 그래도 남는다"


def test_audit_reserve_covers_every_risk_area_of_the_shipped_contract() -> None:
    """예비분이 계약의 위험 영역을 실제로 덮을 수 있어야 한다.

    숫자를 감으로 정하면 감사가 probe 하나를 못 돌려 MISSING_AUDIT 이 난다.
    """
    import pathlib

    from api_doctor.registry.loader import load_dataset

    ds = load_dataset(pathlib.Path("registry/datasets/seoul_library"))
    risks = {r.risk_id for r in ds.contract.audit_requirements}

    # 각 위험 영역을 덮는 probe 중 가장 비싼 것을 골라도 들어가야 한다.
    worst = 0
    for risk in risks:
        costs = [len(pr.runs) for pr in ds.probes.probes if risk in (pr.covers or ())]
        assert costs, f"{risk} 를 덮는 probe 가 없다"
        worst += max(costs)

    assert Limits().sandbox_runs_reserved_for_audit >= worst, (
        f"위험 영역 {sorted(risks)} 를 덮으려면 최소 {worst}회가 필요하다"
    )


def test_finish_slot_can_be_rereserved_without_raising_total() -> None:
    ledger = BudgetLedger(limits=Limits(sandbox_runs=4))
    ledger.spend_sandbox_run(is_finish=True)
    assert ledger.reserve_finish_slot()
    assert ledger.sandbox_runs == 1, "재예약이 총 실행 횟수를 늘리지 않는다"


def test_missing_usage_is_marked_estimated_and_not_refunded() -> None:
    """usage 가 없으면 예약량을 반환하지 않는다 (PRD §4.1)."""
    ledger = BudgetLedger()
    reservation = ledger.reserve_model_call("main", 5000)
    ledger.settle_model_call(reservation, usage_tokens=None)
    assert ledger.tokens_estimated == reservation.tokens
    assert ledger.tokens_reserved == 0
    assert ledger.usage()["tokens_kind"] == "estimated"


def test_patch_limit_is_two() -> None:
    ledger = BudgetLedger()
    ledger.spend_patch()
    ledger.spend_patch()
    with pytest.raises(BudgetExceeded):
        ledger.spend_patch()


def test_cancel_blocks_every_resource_first() -> None:
    """AC-08: 취소는 다른 어떤 판정보다 먼저 온다."""
    ledger = BudgetLedger()
    ledger.cancel.set()
    for verdict in (
        ledger.can_spend_model_call("main"), ledger.can_delegate("data_auditor"),
        ledger.can_run_sandbox(), ledger.can_call_broker(), ledger.can_submit_patch(),
    ):
        assert verdict.reason is DenyReason.CANCELLED


def test_cancel_token_fires_callbacks_once() -> None:
    token = CancelToken()
    fired: list[str] = []
    token.on_cancel(lambda: fired.append("model"))
    token.on_cancel(lambda: fired.append("sandbox"))
    token.set()
    token.set()
    assert fired == ["model", "sandbox"]


def test_cancel_callback_registered_after_cancel_runs_immediately() -> None:
    token = CancelToken()
    token.set()
    fired: list[str] = []
    token.on_cancel(lambda: fired.append("late"))
    assert fired == ["late"], "이미 취소된 뒤 붙은 정리도 실행돼야 한다"


def test_failing_callback_does_not_block_others() -> None:
    token = CancelToken()
    fired: list[str] = []

    def boom() -> None:
        raise RuntimeError("정리 실패")

    token.on_cancel(boom)
    token.on_cancel(lambda: fired.append("survived"))
    token.set()
    assert fired == ["survived"]


def test_usage_reports_per_role_for_profiler_reconciliation() -> None:
    """AC-16: 역할별 호출 수 합계가 원장과 일치해야 한다."""
    ledger = BudgetLedger()
    _spend(ledger, "main", 100)
    _spend(ledger, "main", 100)
    _spend(ledger, "data_auditor", 100)
    usage = ledger.usage()
    assert usage["per_role_calls"]["main"] == 2
    assert sum(usage["per_role_calls"].values()) == usage["model_calls"] == 3


def test_repair_keeps_a_share_of_the_provider_failure_allowance() -> None:
    """조사 역할이 실패 허용량을 다 태워도 수리자는 재시도할 수 있다.

    실측(2026-09-28): 429 가 27회 나 전역 25회를 넘겼고, 그 시점에
    repair_engineer 는 아직 한 번도 성공하지 못한 상태였다. 고칠 단계에
    도달하고도 한 번을 못 돌아보는 것이 가장 비싼 낭비다.
    """
    limits = Limits()
    ledger = BudgetLedger(limits=limits)
    investigation = limits.max_provider_failures - limits.provider_failures_reserved_for_repair
    ledger.failed_attempts = investigation

    assert ledger.provider_failures_exhausted_for("spec_researcher")
    assert ledger.provider_failures_exhausted_for("runtime_diagnostician")
    assert not ledger.provider_failures_exhausted_for("repair_engineer")
    assert not ledger.provider_failures_exhausted_for("data_auditor")

    ledger.failed_attempts = limits.max_provider_failures
    assert ledger.provider_failures_exhausted_for("repair_engineer"), (
        "예비분까지 다 쓰면 수리자도 멈춰야 한다"
    )
    assert ledger.provider_failures_exhausted


def test_single_agent_control_arm_gets_the_whole_failure_allowance() -> None:
    """비교 실험 B 는 역할 분리가 없으므로 예비분을 나눌 상대가 없다 (PRD §7.3)."""
    limits = Limits()
    ledger = BudgetLedger(limits=limits)
    ledger.failed_attempts = limits.max_provider_failures - 1
    assert not ledger.provider_failures_exhausted_for("single_agent")
