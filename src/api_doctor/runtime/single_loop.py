"""비교 실험 B — 단일 에이전트 복구 루프 (PRD §7.3).

**제품 구조가 아니다.** 팀 분리의 효용을 재기 위한 대조군이다.
같은 모델·자료·도구·고정 검증·예산 상한을 쓰되 역할을 나누지 않는다.

이 경로로 성공해도 제품을 단일 에이전트로 되돌리는 결정이 아니다 (PRD §7.3).
"""

from __future__ import annotations

import json
from typing import Any

from ..agents.prompts import BY_AGENT
from ..agents.protocol import ProtocolError, parse_agent_turn
from ..model.gateway import ModelGateway, ModelUnavailable
from ..tools.gateway import Lease, ToolContext, ToolError, ToolGateway, make_lease
from ..tools.impl import register_all
from .budget import BudgetExceeded, Cancelled
from .deep_loop import _Delegation, _record_findings, _to_result
from .events import EventType, RunStatus, Stage
from .gate import GateDecision
from .orchestrator import OrchestrationResult, _run_gate
from .session import RunSession

AGENT_ID = "single_agent"
MAX_TURNS = 20


def orchestrate_single(
    *, session: RunSession, model: ModelGateway
) -> OrchestrationResult:
    """단일 에이전트가 혼자 복구를 시도한 뒤 같은 고정 게이트로 간다."""
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    gateway.on_tool_call = lambda entry: session.events.append(
        EventType.TOOL_CALL,
        f"{entry['agent_id']} → {entry['tool']}"
        + ("" if entry["allowed"] else f" ({entry['reason']})"),
        **entry,
    )
    session.events.append(
        EventType.STAGE_CHANGED, "단일 에이전트 대조군을 시작합니다.",
        **{"from": str(Stage.BASELINE), "to": str(Stage.DELEGATING),
           "harness": "single", "note": "비교 실험 B. 제품 구조가 아닙니다."},
    )

    def context() -> ToolContext:
        return ToolContext(
            run_id=session.run_id, task_id="single", agent_id=AGENT_ID,
            candidate_hash=session.current_hash,
            contract_hash=session.dataset.contract.contract_hash,
            snapshot_hash=session.snapshot.snapshot_hash,
            lease=make_lease(session.ledger, AGENT_ID),
            allowed_probe_ids=tuple(
                p.probe_id for p in session.dataset.probes.probes
            ),
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": BY_AGENT[AGENT_ID]},
        {"role": "user", "content": _opening(session)},
    ]
    tool_calls: list[str] = []
    turns = 0
    result = None

    while turns < MAX_TURNS:
        if not session.ledger.can_spend_model_call(AGENT_ID):
            break
        try:
            reply = model.complete(agent_id=AGENT_ID, messages=messages)
        except (BudgetExceeded, ModelUnavailable):
            break
        except Cancelled:
            session.forced_halt = str(RunStatus.CANCELLED)
            break
        turns += 1
        messages.append({"role": "assistant", "content": reply})

        try:
            turn = parse_agent_turn(reply)
        except ProtocolError as exc:
            messages.append({
                "role": "user",
                "content": f"출력 형식이 올바르지 않습니다: {exc}\n"
                           "JSON 객체 하나만 다시 출력하세요.",
            })
            continue

        if turn.result is not None:
            result = turn.result
            break

        request = turn.tool_request
        assert request is not None
        tool_calls.append(request.tool)
        bound = gateway.bind(context())
        implementation = bound.get(request.tool)
        if implementation is None:
            messages.append({
                "role": "user",
                "content": f"`{request.tool}` 은 사용할 수 없습니다. "
                           f"사용 가능: {sorted(bound)}",
            })
            continue
        try:
            payload = json.dumps(
                implementation(**request.args), ensure_ascii=False
            )[:6000]
        except ToolError as exc:
            payload = json.dumps(
                {"error": exc.code, "message": str(exc)}, ensure_ascii=False
            )
        except Cancelled:
            session.forced_halt = str(RunStatus.CANCELLED)
            break
        messages.append({"role": "user", "content": f"도구 결과:\n{payload}"})

    agent_result = result if result is not None else _to_result("")
    if agent_result.findings:
        _record_findings(session, agent_result)

    delegation = _Delegation(
        agent_id=AGENT_ID, objective="단일 에이전트 복구 (비교 실험 B)",
        result=agent_result, tool_calls=tool_calls, turns=turns,
    )
    session.events.append(
        EventType.DELEGATION_FINISHED,
        f"{AGENT_ID}: {agent_result.summary[:110]}",
        agent_id=AGENT_ID, outcome=str(agent_result.outcome),
        tool_calls=tool_calls, turns=turns, harness="single",
    )

    gate: GateDecision = _run_gate(session)
    return OrchestrationResult(
        status=gate.status or RunStatus.VERIFICATION_INCONCLUSIVE,
        decision=gate, lead_turns=turns, delegations=[delegation],
        detail=gate.detail, tool_log=gateway.call_log,
    )


def _opening(session: RunSession) -> str:
    payload = {
        "run_id": session.run_id,
        "dataset": session.dataset.dataset_id,
        "contract": session.dataset.contract.neutral_summary(),
        "available_probes": [p.probe_id for p in session.dataset.probes.probes],
        "budget_remaining": session.ledger.remaining(),
        "situation": (
            "원본 연결 코드가 고정 검증을 통과하지 못했습니다. "
            "원인을 찾아 고치고, 데이터가 빠지지 않았는지 직접 확인한 뒤 "
            "request_finish 로 종료 검증을 예약하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
