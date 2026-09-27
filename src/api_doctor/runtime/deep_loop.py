"""deepagents 하네스로 도는 복구 루프 (PRD §5.3.4 우선 구현안).

main 은 Deep Agents 의 `task` 로 위임하고, 런타임은 그 도구를 감싸
중복 위임·예산·기록을 강제한다. 종료 판정은 여전히 고정 게이트가 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig

from ..agents.deep import DeepTeam, build_team
from ..agents.lead import DelegationKey, LeadState, NoNewEvidence, _shape
from ..agents.protocol import AgentResult, Outcome, parse_agent_result
from ..jsonio import ProtocolError, parse_json_object
from ..model.gateway import ModelGateway
from ..tools.gateway import ToolGateway
from ..tools.impl import register_all
from .budget import BudgetExceeded, Cancelled
from .events import EventType, RunStatus, Stage
from .gate import GateDecision
from .orchestrator import OrchestrationResult, _run_gate
from .session import AuditRecord, RunSession

MAX_LEAD_STEPS = 24


@dataclass(slots=True)
class _Delegation:
    """위임 1건의 기록.

    builtin 하네스의 `SubAgentRun` 과 **같은 모양**을 낸다 — 보고서가
    두 하네스를 구분하지 않고 읽을 수 있어야 하기 때문이다.
    """

    agent_id: str
    objective: str
    result: AgentResult
    tool_calls: list[str] = field(default_factory=list)
    turns: int = 1

    @property
    def envelope(self) -> Any:
        """`run.envelope.agent_id` 접근을 위한 자기 참조."""
        return self


def orchestrate_deep(
    *, session: RunSession, model: ModelGateway
) -> OrchestrationResult:
    """deepagents 팀을 돌린 뒤 고정 게이트로 판정한다."""
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    gateway.on_tool_call = lambda entry: session.events.append(
        EventType.TOOL_CALL,
        f"{entry['agent_id']} → {entry['tool']}"
        + ("" if entry["allowed"] else f" ({entry['reason']})"),
        **entry,
    )

    team = build_team(session, gateway, model)
    session.events.append(
        EventType.STAGE_CHANGED, "deepagents 팀을 구성했습니다.",
        **{"from": str(Stage.BASELINE), "to": str(Stage.DELEGATING),
           "harness": "deepagents", "inventory": team.inventory},
    )

    state = LeadState()
    delegations: list[_Delegation] = []

    status: RunStatus | None = None
    transcript: list[Any] = []
    try:
        result = team.agent.invoke(
            {"messages": [{"role": "user", "content": _opening(session)}]},
            {"recursion_limit": MAX_LEAD_STEPS,
             "configurable": {"thread_id": session.run_id}},
        )
        transcript = list(result.get("messages") or [])
    except Cancelled:
        session.forced_halt = str(RunStatus.CANCELLED)
        status = RunStatus.CANCELLED
    except BudgetExceeded as exc:
        session.events.append(EventType.BUDGET_EVENT, str(exc)[:160])
        status = RunStatus.BUDGET_EXHAUSTED
    except Exception as exc:  # noqa: BLE001 - 하네스 예외도 같은 게이트로 간다
        session.events.append(
            EventType.PLAN_REVISED,
            f"하네스가 예외로 끝났습니다: {type(exc).__name__}: {str(exc)[:160]}",
            error="agent_protocol_error",
        )

    team.objectives.update(_objectives(transcript))
    returns = _subagent_returns(transcript)
    delegations = _reconstruct_delegations(session, gateway, team, returns)
    for text in returns.get("data_auditor", []):
        _record(session, "data_auditor", text)
    for run in delegations:
        session.events.append(
            EventType.DELEGATION_FINISHED,
            f"{run.agent_id}: {run.result.summary[:110]}",
            agent_id=run.agent_id, tool_calls=run.tool_calls,
            outcome=str(run.result.outcome),
            candidate_hash=session.current_hash, harness="deepagents",
        )

    gate: GateDecision = _run_gate(session)
    final = status or gate.status or RunStatus.VERIFICATION_INCONCLUSIVE
    if status in (RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED):
        final = status
    return OrchestrationResult(
        status=final, decision=gate,
        lead_turns=len(delegations),
        delegations=delegations, detail=gate.detail,
    )


def _opening(session: RunSession) -> str:
    contract = session.dataset.contract
    payload = {
        "run_id": session.run_id,
        "dataset": session.dataset.dataset_id,
        "contract": contract.neutral_summary(),
        "current_candidate_hash": session.current_hash[:23] + "…",
        "budget_remaining": session.ledger.remaining(),
        "situation": (
            "원본 연결 코드가 고정 검증을 통과하지 못했습니다. "
            "필요한 조사만 골라 위임하고, 수리 후 독립 감사를 받은 뒤 "
            "request_finish 로 종료 검증을 예약하세요."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _to_result(text: str) -> AgentResult:
    """서브에이전트의 반환 본문을 공통 구조로 옮긴다.

    구조화되지 않은 평문이면 `need_more_evidence` 로 둔다 — 구조를 지키지
    않은 반환을 completed 로 세면 기여가 있었던 것처럼 보인다.
    """
    try:
        data = parse_json_object(text)
    except ProtocolError:
        return AgentResult(
            outcome=Outcome.NEED_MORE_EVIDENCE,
            summary=(text or "구조화 반환이 없습니다.")[:1200],
        )
    try:
        return parse_agent_result(data)
    except ProtocolError as exc:
        return AgentResult(
            outcome=Outcome.FAILED,
            summary=f"구조화 반환이 계약을 만족하지 않습니다: {exc}",
        )


def _subagent_returns(transcript: list[Any]) -> dict[str, list[str]]:
    """`task` 호출과 그 결과를 짝지어 역할별 반환 본문을 모은다.

    main 의 대화에는 위임 요청(AIMessage.tool_calls)과 그 결과(ToolMessage)가
    순서대로 남으므로, 호출 id 로 이어 붙이면 누가 무엇을 냈는지 알 수 있다.
    """
    pending: dict[str, str] = {}
    returns: dict[str, list[str]] = {}
    for message in transcript:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") == "task":
                agent_id = str((call.get("args") or {}).get("subagent_type", ""))
                if agent_id:
                    pending[str(call.get("id"))] = agent_id
        call_id = getattr(message, "tool_call_id", None)
        if call_id and call_id in pending:
            agent_id = pending.pop(call_id)
            returns.setdefault(agent_id, []).append(
                _extract_text(getattr(message, "content", ""))
            )
    return returns


def _objectives(transcript: list[Any]) -> dict[str, str]:
    objectives: dict[str, str] = {}
    for message in transcript:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") == "task":
                args = call.get("args") or {}
                agent_id = str(args.get("subagent_type", ""))
                if agent_id and agent_id not in objectives:
                    objectives[agent_id] = str(args.get("description", ""))
    return objectives


def _reconstruct_delegations(
    session: RunSession,
    gateway: ToolGateway,
    team: DeepTeam,
    returns: dict[str, list[str]],
) -> list[_Delegation]:
    """게이트웨이 호출 기록에서 위임을 복원한다.

    deepagents 의 `task` 는 LangGraph 런타임 주입을 요구해 밖에서 감쌀 수 없다.
    대신 네이티브로 돌리고, 전문가의 **모든 도구 호출이 우리 게이트웨이를
    지난다**는 사실을 이용해 누가 무엇을 했는지 복원한다.

    경계는 이 복원에 의존하지 않는다 — 위임 대상은 deepagents 등록으로,
    권한은 ToolGateway 로, 호출 수는 원장으로, 판정은 고정 게이트로 강제된다.
    """
    runs: list[_Delegation] = []
    current: _Delegation | None = None
    for entry in gateway.call_log:
        agent_id = entry["agent_id"]
        if agent_id == "main":
            continue
        if current is None or current.agent_id != agent_id:
            texts = returns.get(agent_id) or []
            raw = texts[len(runs) if len(runs) < len(texts) else -1] if texts else ""
            current = _Delegation(
                agent_id=agent_id,
                objective=team.objectives.get(agent_id, ""),
                result=_to_result(raw),
            )
            runs.append(current)
        if entry["allowed"]:
            current.tool_calls.append(entry["tool"])
    return runs


def _extract_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _record(session: RunSession, agent_id: str, summary: str) -> None:
    """감사 반환이면 세션 상태에 반영한다.

    구조화 findings 가 없으면 감사 기록을 만들지 않는다 — 찬성 문구만으로는
    감사가 완료되지 않기 때문이다 (AC-04).
    """
    if agent_id != "data_auditor":
        return
    try:
        data = parse_json_object(summary)
    except ProtocolError:
        return
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        conclusion = str(raw.get("conclusion", "")).strip()
        if conclusion not in ("no_issue", "issue", "inconclusive"):
            continue
        session.audit_records.append(
            AuditRecord(
                candidate_hash=session.current_hash,
                risk_id=str(raw.get("risk_id", "")).strip(),
                hypothesis=str(raw.get("hypothesis", ""))[:600],
                invariant=str(raw.get("invariant", ""))[:120],
                evidence_ids=tuple(str(x) for x in (raw.get("evidence_ids") or [])),
                probe_result_ids=tuple(
                    str(x) for x in (raw.get("probe_result_ids") or [])
                ),
                conclusion=conclusion,
            )
        )
    session.events.append(
        EventType.AUDIT_RETURNED,
        f"감사가 {len(session.audit_for(session.current_hash))}개 항목을 반환했습니다.",
        candidate_hash=session.current_hash,
    )
