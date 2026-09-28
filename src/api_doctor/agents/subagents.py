"""전문 역할의 제한된 agent loop (FR-008~011).

각 전문가는 자기에게 바인딩된 도구만 갖고, `task` 같은 위임 도구를 받지 않는다.
하위 재위임이 구조적으로 불가능한 이유다 (AC-01).

모든 모델 호출은 `ModelGateway` 를 지난다. 경로가 하나뿐이라는 것이
FR-003("모든 SDK 요청이 동일 gateway 를 통과")의 보증이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..model.gateway import ModelGateway, ModelUnavailable
from ..runtime.budget import BudgetExceeded, Cancelled
from ..runtime.envelope import TaskEnvelope, build_context
from ..runtime.events import EventType
from ..runtime.session import RunSession
from ..tools.gateway import ToolError, ToolGateway
from .prompts import BY_AGENT
from .protocol import (
    AgentResult, Outcome, ProtocolError, parse_agent_turn,
)

MAX_TURNS = 6


@dataclass(slots=True)
class SubAgentRun:
    envelope: TaskEnvelope
    result: AgentResult
    turns: int
    tool_calls: list[str]


def run_subagent(
    *,
    session: RunSession,
    gateway: ToolGateway,
    model: ModelGateway,
    envelope: TaskEnvelope,
) -> SubAgentRun:
    """전문 역할 1건을 실행한다.

    lease 의 모델 호출 수와 전체 예산 중 **작은 쪽**에서 멈춘다.
    """
    context = build_context(session, envelope)
    tools = gateway.bind(context)
    agent_id = envelope.agent_id

    session.events.append(
        EventType.DELEGATION_STARTED,
        f"{agent_id} 에게 위임했습니다: {envelope.objective[:100]}",
        task_id=envelope.task_id, agent_id=agent_id,
        lease=envelope.lease.to_json(), candidate_hash=envelope.candidate_hash,
        allowed_tools=sorted(tools), evidence_ids=list(envelope.evidence_ids),
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": BY_AGENT[agent_id]},
        {"role": "user", "content": _opening(envelope, tools)},
    ]
    tool_calls: list[str] = []
    turns = 0
    budget = min(envelope.lease.model_calls, MAX_TURNS)

    while turns < budget:
        verdict = session.ledger.can_spend_model_call(agent_id)
        if not verdict:
            return _finish(
                session, envelope, turns, tool_calls,
                AgentResult(
                    outcome=Outcome.BLOCKED,
                    summary=f"예산이 소진되어 중단했습니다: {verdict.detail}",
                    unknowns=("조사를 끝내지 못했습니다.",),
                ),
            )

        try:
            reply = model.complete(agent_id=agent_id, messages=messages)
        except (BudgetExceeded, ModelUnavailable) as exc:
            return _finish(
                session, envelope, turns, tool_calls,
                AgentResult(
                    outcome=Outcome.BLOCKED,
                    summary=f"모델 호출이 실패했습니다: {exc}",
                    unknowns=("조사를 끝내지 못했습니다.",),
                ),
            )
        except Cancelled:
            raise
        turns += 1
        messages.append({"role": "assistant", "content": reply})

        try:
            turn = parse_agent_turn(
                reply, strict_findings=agent_id == "data_auditor"
            )
        except ProtocolError as exc:
            # 형식 재시도 1회도 같은 예산에 포함된다 (PRD §5.1.2).
            messages.append({
                "role": "user",
                "content": f"출력 형식이 올바르지 않습니다: {exc}\n"
                           "JSON 객체 하나만 다시 출력하세요.",
            })
            continue

        if turn.result is not None:
            return _finish(session, envelope, turns, tool_calls, turn.result)

        request = turn.tool_request
        assert request is not None
        tool_calls.append(request.tool)
        implementation = tools.get(request.tool)
        if implementation is None:
            messages.append({
                "role": "user",
                "content": f"`{request.tool}` 은 네 권한에 없습니다. "
                           f"사용 가능: {sorted(tools)}",
            })
            continue

        try:
            observation = implementation(**request.args)
            payload = json.dumps(observation, ensure_ascii=False)[:6000]
        except ToolError as exc:
            payload = json.dumps(
                {"error": exc.code, "message": str(exc)}, ensure_ascii=False
            )
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 도구 실패를 모델에 구조화해 전달
            payload = json.dumps(
                {"error": "TOOL_FAILED", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        messages.append({"role": "user", "content": f"도구 결과:\n{payload}"})

    return _finish(
        session, envelope, turns, tool_calls,
        AgentResult(
            outcome=Outcome.NEED_MORE_EVIDENCE,
            summary=f"{turns}턴 안에 결론을 내지 못했습니다.",
            unknowns=("턴 상한에 도달했습니다.",),
        ),
    )


def _opening(envelope: TaskEnvelope, tools: dict[str, Any]) -> str:
    payload = envelope.to_json()
    payload["available_tools"] = sorted(tools)
    return (
        "다음 작업을 수행하세요.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _finish(
    session: RunSession,
    envelope: TaskEnvelope,
    turns: int,
    tool_calls: list[str],
    result: AgentResult,
) -> SubAgentRun:
    """결과를 기록하고, 감사 결과면 세션 상태에 반영한다."""
    from ..runtime.session import AuditRecord

    if envelope.agent_id == "data_auditor":
        for finding in result.findings:
            session.audit_records.append(
                AuditRecord(
                    candidate_hash=envelope.candidate_hash,
                    risk_id=finding.risk_id,
                    hypothesis=finding.hypothesis,
                    invariant=finding.invariant,
                    evidence_ids=finding.evidence_ids,
                    probe_result_ids=finding.probe_result_ids,
                    conclusion=finding.conclusion,
                )
            )
        session.events.append(
            EventType.AUDIT_RETURNED,
            f"감사가 {len(result.findings)}개 항목을 반환했습니다.",
            task_id=envelope.task_id, candidate_hash=envelope.candidate_hash,
            findings=[f.to_json() for f in result.findings],
        )

    session.events.append(
        EventType.DELEGATION_FINISHED,
        f"{envelope.agent_id}: {result.summary[:120]}",
        task_id=envelope.task_id, agent_id=envelope.agent_id,
        outcome=str(result.outcome), turns=turns, tool_calls=tool_calls,
        candidate_hash=envelope.candidate_hash,
    )
    return SubAgentRun(
        envelope=envelope, result=result, turns=turns, tool_calls=tool_calls
    )
