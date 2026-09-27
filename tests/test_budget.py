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
    """종료 검증용 실행 1회는 양도 불가다 (PRD §4.1)."""
    ledger = BudgetLedger(limits=Limits(sandbox_runs=3))
    ledger.spend_sandbox_run()
    ledger.spend_sandbox_run()
    assert ledger.usable_sandbox_runs == 0
    assert ledger.can_run_sandbox().reason is DenyReason.SANDBOX_EXHAUSTED
    assert ledger.can_run_sandbox(is_finish=True), "종료 검증은 예약분을 쓴다"


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
