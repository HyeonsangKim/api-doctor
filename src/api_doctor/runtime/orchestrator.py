"""delegating 루프 (FR-007, FR-012, FR-013, 아키텍처 §4.1).

main 이 담당자와 질문을 정하고, 런타임이 envelope·lease·권한을 발급하고,
종료 게이트가 판정한다. 게이트는 우회할 수 없다 — main 의 종료 요청,
일반 텍스트 종료, agent 예외, 루프 상한이 전부 여기로 수렴한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.inventory import assert_inventory
from ..agents.lead import NoNewEvidence, RecoveryLead, build_lead
from ..agents.protocol import LeadAction, LeadDecision, ProtocolError
from ..agents.subagents import SubAgentRun, run_subagent
from ..model.gateway import ModelGateway, ModelUnavailable
from ..tools.gateway import ToolGateway
from ..tools.impl import register_all
from .budget import BudgetExceeded, Cancelled
from .envelope import build_envelope
from .events import EventType, RunStatus, Stage
from .gate import GateDecision, evaluate
from .session import RunSession

MAX_LEAD_TURNS = 12


@dataclass(slots=True)
class OrchestrationResult:
    status: RunStatus
    decision: GateDecision | None
    lead_turns: int
    delegations: list[SubAgentRun] = field(default_factory=list)
    detail: str = ""
    tool_log: list[dict[str, Any]] = field(default_factory=list)


def orchestrate(
    *, session: RunSession, model: ModelGateway
) -> OrchestrationResult:
    """baseline 이 결함을 확인한 뒤의 복구 루프."""
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    gateway.on_tool_call = lambda entry: session.events.append(
        EventType.TOOL_CALL,
        f"{entry['agent_id']} → {entry['tool']}"
        + ("" if entry["allowed"] else f" ({entry['reason']})"),
        **entry,
    )
    # 초기화 직후 실제 발급 도구를 확인한다. 하나라도 어긋나면 실행하지 않는다.
    inventory = assert_inventory(gateway)
    session.events.append(
        EventType.STAGE_CHANGED, "도구 경계를 확인했습니다.",
        **{"from": str(Stage.BASELINE), "to": str(Stage.DELEGATING),
           "inventory": inventory},
    )

    lead: RecoveryLead = build_lead(session, model)
    observations: list[str] = []
    delegations: list[SubAgentRun] = []
    turns = 0
    finish_requested = False
    stop_status: RunStatus | None = None

    while turns < MAX_LEAD_TURNS:
        verdict = session.ledger.can_spend_model_call("main")
        if not verdict:
            observations.append(f"main 의 예산이 소진되었습니다: {verdict.detail}")
            stop_status = _status_for(verdict.reason)
            break

        try:
            decision = lead.decide(observations)
        except ProtocolError as exc:
            session.events.append(
                EventType.PLAN_REVISED,
                f"main 의 출력을 해석하지 못했습니다: {exc}", error="agent_protocol_error",
            )
            stop_status = RunStatus.NEEDS_USER_ACTION
            break
        except (BudgetExceeded, ModelUnavailable) as exc:
            observations.append(str(exc))
            stop_status = RunStatus.BUDGET_EXHAUSTED if isinstance(
                exc, BudgetExceeded) else RunStatus.EXTERNAL_UNAVAILABLE
            break
        turns += 1

        if decision.action is LeadAction.REVISE_PLAN:
            lead.state.plan = list(decision.plan)
            session.events.append(
                EventType.PLAN_REVISED, decision.reason[:150] or "계획을 갱신했습니다.",
                plan=list(decision.plan), reason=decision.reason,
            )
            observations.append(f"계획을 갱신했습니다: {decision.reason[:120]}")
            continue

        if decision.action is LeadAction.REQUEST_STOP:
            stop_status = _stop_status(decision)
            session.events.append(
                EventType.PLAN_REVISED,
                f"main 이 중단을 요청했습니다: {decision.stop_reason}",
                reason=decision.reason, stop_reason=decision.stop_reason,
            )
            break

        if decision.action is LeadAction.REQUEST_FINISH:
            finish_requested = True
            session.events.append(
                EventType.STAGE_CHANGED, "main 이 종료 검증을 요청했습니다.",
                **{"from": str(Stage.DELEGATING), "to": str(Stage.CHECKING),
                   "reason": decision.reason},
            )
            gate = _run_gate(session)
            if gate.terminal:
                return OrchestrationResult(
                    status=gate.status or RunStatus.VERIFICATION_INCONCLUSIVE,
                    decision=gate, lead_turns=turns, delegations=delegations,
                    detail=gate.detail, tool_log=gateway.call_log,
                )
            observations.append(
                f"종료 게이트가 되돌려보냈습니다 ({gate.rejection}): {gate.detail[:200]}"
            )
            continue

        # ---- 위임 ----
        try:
            lead.check_new_evidence(decision)
        except NoNewEvidence as exc:
            observations.append(str(exc))
            if lead.repeated_rejection(decision) and "종료" in str(exc):
                stop_status = RunStatus.VERIFICATION_INCONCLUSIVE
                break
            continue

        try:
            session.ledger.spend_delegation(decision.agent_id)
        except BudgetExceeded as exc:
            observations.append(f"위임 예산이 부족합니다: {exc}")
            session.events.append(
                EventType.DELEGATION_REJECTED, str(exc)[:150],
                reason=str(exc.verdict.reason), agent_id=decision.agent_id,
            )
            continue

        session.events.append(
            EventType.DELEGATION_REQUESTED,
            f"{decision.agent_id}: {decision.objective[:110]}",
            agent_id=decision.agent_id, objective=decision.objective,
            evidence_ids=list(decision.evidence_ids), reason=decision.reason,
        )
        envelope = build_envelope(
            session, agent_id=decision.agent_id,
            objective=decision.objective,
            evidence_ids=decision.evidence_ids,
        )
        run = run_subagent(
            session=session, gateway=gateway, model=model, envelope=envelope
        )
        delegations.append(run)
        observations.append(
            f"[{decision.agent_id}] {run.result.outcome}: {run.result.summary[:220]}"
        )
        for unknown in run.result.unknowns[:3]:
            lead.state.open_questions.append(f"{decision.agent_id}: {unknown}")

    # ---- 루프를 빠져나온 모든 경로가 같은 게이트로 간다 ----
    if stop_status is not None:
        session.forced_halt = (
            str(stop_status)
            if stop_status in (RunStatus.BUDGET_EXHAUSTED, RunStatus.CANCELLED,
                               RunStatus.POLICY_BLOCKED)
            else None
        )
        if session.forced_halt is None:
            gate = _run_gate(session, allow_finish=False)
            if gate.terminal and gate.status is RunStatus.VERIFIED_REPAIRED:
                # 중단을 요청했더라도 실제로 통과했다면 게이트 판정을 따른다.
                return OrchestrationResult(
                    status=gate.status, decision=gate, lead_turns=turns,
                    delegations=delegations, detail=gate.detail,
                    tool_log=gateway.call_log,
                )
            return OrchestrationResult(
                status=stop_status, decision=gate, lead_turns=turns,
                delegations=delegations, detail=gate.detail,
                tool_log=gateway.call_log,
            )

    gate = _run_gate(session, allow_finish=not finish_requested)
    status = gate.status or RunStatus.VERIFICATION_INCONCLUSIVE
    return OrchestrationResult(
        status=status, decision=gate, lead_turns=turns,
        delegations=delegations, detail=gate.detail, tool_log=gateway.call_log,
    )


def _run_gate(session: RunSession, *, allow_finish: bool = True) -> GateDecision:
    """종료 검증을 실행하고 게이트를 평가한다.

    이미 강제 중단된 상태에서는 새 코드를 실행하지 않는다 (PRD §5.3.3).
    """
    verdict = None
    if session.forced_halt is None and allow_finish:
        try:
            records, error, *_ = session.execute_candidate(
                session.current_hash, session.dataset.contract.query, is_finish=True
            )
            verdict = session.verifier.verify(
                records=records, execution_error=error,
                candidate_hash=session.current_hash,
                snapshot_hash=session.snapshot.snapshot_hash,
            )
            session.final_verdict = verdict
            session.write_verdict(verdict, "final")
            for result in verdict.results:
                session.events.append(
                    EventType.CHECK_RESULT, f"{result.kind}: {result.summary}",
                    kind=str(result.kind), outcome=str(result.outcome),
                    candidate_hash=verdict.candidate_hash, metrics=result.metrics,
                )
        except (BudgetExceeded, Cancelled) as exc:
            session.events.append(
                EventType.BUDGET_EVENT, f"종료 검증을 실행하지 못했습니다: {exc}",
            )
            verdict = session.final_verdict
    else:
        verdict = session.final_verdict

    decision = evaluate(
        session, candidate_hash=session.current_hash, final_verdict=verdict
    )
    session.events.append(
        EventType.GATE_EVALUATED,
        decision.detail[:200] or "게이트를 평가했습니다.",
        **decision.to_json(),
    )
    return decision


def _stop_status(decision: LeadDecision) -> RunStatus:
    try:
        return RunStatus(decision.stop_reason)
    except ValueError:
        return RunStatus.NEEDS_USER_ACTION


def _status_for(reason: Any) -> RunStatus:
    mapping = {
        "cancelled": RunStatus.CANCELLED,
        "time_exhausted": RunStatus.BUDGET_EXHAUSTED,
        "tokens_exhausted": RunStatus.BUDGET_EXHAUSTED,
        "calls_exhausted": RunStatus.BUDGET_EXHAUSTED,
        "role_calls_exhausted": RunStatus.BUDGET_EXHAUSTED,
    }
    return mapping.get(str(reason), RunStatus.VERIFICATION_INCONCLUSIVE)
