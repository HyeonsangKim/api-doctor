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
# PRD §5.3.3: 최종 검증 실패 후 main 에 돌아가는 개발 루프.
# 전체 시간·2패치·현재 hash 조건을 만족할 때만 허용한다.
MAX_ATTEMPTS = 2
# 개발 루프로 되돌려보낼 수 있는 종료 상태.
_RETRYABLE = frozenset({
    RunStatus.VERIFICATION_INCONCLUSIVE,
    RunStatus.VERIFICATION_FAILED,
})


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
    gate: GateDecision | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            team.agent.invoke(
                {"messages": [{"role": "user",
                               "content": _opening(session, gate)}]},
                {"recursion_limit": MAX_LEAD_STEPS,
                 "configurable": {"thread_id": f"{session.run_id}-{attempt}"}},
            )
        except Cancelled:
            session.forced_halt = str(RunStatus.CANCELLED)
            status = RunStatus.CANCELLED
            break
        except BudgetExceeded as exc:
            session.events.append(EventType.BUDGET_EVENT, str(exc)[:160])
            status = RunStatus.BUDGET_EXHAUSTED
            break
        except Exception as exc:  # noqa: BLE001 - 하네스 예외도 같은 게이트로 간다
            session.events.append(
                EventType.PLAN_REVISED,
                f"하네스가 예외로 끝났습니다: {type(exc).__name__}: {str(exc)[:160]}",
                error="agent_protocol_error",
            )

        # 감사 findings 를 반영한 뒤 게이트를 본다.
        for run in _delegations_from_guard(team, gateway):
            if run.agent_id == "data_auditor" and run.result.findings:
                _record_findings(session, run.result)
        gate = _run_gate(session)
        # PRD §5.3.3 의 개발 루프는 "최종 검증 **실패** 후" 를 위한 것이다.
        # 판정 보류만 되돌려보내고 검증 실패를 그대로 끝내면, 부분 수리가
        # 두 번째 기회를 못 받는다 — 실측에서 결함 둘 중 하나만 고친 후보가
        # 이대로 종료됐다.
        if gate.terminal and gate.status not in _RETRYABLE:
            break
        if not _can_retry(session, attempt):
            break
        session.events.append(
            EventType.PLAN_REVISED,
            f"게이트가 되돌려보냈습니다. main 으로 돌아갑니다 "
            f"({attempt + 2}/{MAX_ATTEMPTS}).",
            reason=gate.detail[:200], rejection=str(gate.rejection),
        )

    delegations = _delegations_from_guard(team, gateway)
    for run in delegations:
        session.events.append(
            EventType.DELEGATION_FINISHED,
            f"{run.agent_id}: {run.result.summary[:110]}",
            agent_id=run.agent_id, tool_calls=run.tool_calls,
            outcome=str(run.result.outcome),
            candidate_hash=session.current_hash, harness="deepagents",
        )

    if gate is None:
        gate = _run_gate(session)
    final = status or gate.status or RunStatus.VERIFICATION_INCONCLUSIVE
    if status in (RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED):
        final = status
    return OrchestrationResult(
        status=final, decision=gate,
        lead_turns=len(delegations),
        delegations=delegations, detail=gate.detail,
        tool_log=gateway.call_log,
    )


def _can_retry(session: RunSession, attempt: int) -> bool:
    """개발 루프로 돌아갈 수 있는가 (PRD §5.3.3).

    전체 시간·패치 상한·예산이 남아 있어야 한다.
    """
    if attempt + 1 >= MAX_ATTEMPTS:
        return False
    if session.forced_halt is not None:
        return False
    if session.ledger.patches >= session.ledger.limits.patches:
        return False
    if not session.ledger.can_spend_model_call("main"):
        return False
    return bool(session.ledger.reserve_finish_slot())


def _opening(session: RunSession, gate: GateDecision | None = None) -> str:
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
    if gate is not None:
        failed_checks = [
            {"kind": check["kind"], "summary": check["summary"]}
            for check in (gate.verdict.to_json()["checks"] if gate.verdict else [])
            if check["outcome"] != "pass"
        ]
        payload["previous_attempt"] = {
            "status": str(gate.status) if gate.status else None,
            "rejection": str(gate.rejection) if gate.rejection else None,
            "failed_checks": failed_checks,
            "detail": gate.detail[:400],
            "what_to_do": (
                "종료 게이트가 되돌려보냈습니다. 위 failed_checks 를 보고 "
                "**남은 결함을 마저 고치세요.** 이미 끝난 조사를 반복하지 말고 "
                "repair_engineer 에게 구체적인 수정 지시를 내리세요."
            ),
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _to_result(text: str, *, strict_findings: bool = False) -> AgentResult:
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
        return parse_agent_result(data, strict_findings=strict_findings)
    except ProtocolError as exc:
        return AgentResult(
            outcome=Outcome.FAILED,
            summary=f"구조화 반환이 계약을 만족하지 않습니다: {exc}",
        )


def _delegations_from_guard(
    team: DeepTeam, gateway: ToolGateway
) -> list[_Delegation]:
    """미들웨어가 가로챈 위임을 그대로 쓴다.

    게이트웨이 호출 기록에서 복원하던 앞선 방식은 **도구를 한 번도 쓰지 않은
    역할을 통째로 빠뜨렸다.** FR-017 이 드러내려는 "이름만 등장하는 역할" 을
    오히려 숨기는 셈이라 바꿨다.

    거절된 위임도 기록에 남긴다 — 무엇을 시도했다 막혔는지가 증거다.
    """
    guard = team.guard
    if guard is None:
        return []

    # 도구 호출을 역할별 순서대로 각 위임에 나눠 붙인다.
    # main 은 순차 실행이므로 게이트웨이 로그의 순서가 곧 위임 순서다.
    accepted = [r for r in guard.records if r.accepted]
    buckets: dict[str, list[list[str]]] = {}
    for record in accepted:
        buckets.setdefault(record.agent_id, []).append([])

    cursor: dict[str, int] = {}
    previous: str | None = None
    for entry in gateway.call_log:
        agent_id = entry["agent_id"]
        if agent_id == "main" or agent_id not in buckets:
            continue
        if agent_id != previous:
            cursor[agent_id] = cursor.get(agent_id, -1) + 1
            previous = agent_id
        index = min(cursor[agent_id], len(buckets[agent_id]) - 1)
        if entry["allowed"]:
            buckets[agent_id][index].append(entry["tool"])

    taken: dict[str, int] = {}
    runs: list[_Delegation] = []
    for record in guard.records:
        if not record.accepted:
            runs.append(_Delegation(
                agent_id=record.agent_id, objective=record.objective,
                result=AgentResult(
                    outcome=Outcome.BLOCKED,
                    summary=f"위임이 거절되었습니다: {record.reason}",
                ),
                tool_calls=[], turns=0,
            ))
            continue
        index = taken.get(record.agent_id, 0)
        taken[record.agent_id] = index + 1
        tools = buckets.get(record.agent_id, [[]])[
            min(index, len(buckets[record.agent_id]) - 1)
        ]
        runs.append(_Delegation(
            agent_id=record.agent_id, objective=record.objective,
            result=_to_result(
                record.returned,
                strict_findings=record.agent_id == "data_auditor",
            ),
            tool_calls=list(tools),
        ))
    return runs


def _extract_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _record_findings(session: RunSession, result: AgentResult) -> None:
    """감사자의 구조화 findings 를 세션에 반영한다.

    구조화되지 않은 찬성 문구는 기록하지 않는다 — 그것만으로는 감사가
    완료되지 않기 때문이다 (AC-04).
    """
    for finding in result.findings:
        session.audit_records.append(
            AuditRecord(
                candidate_hash=session.current_hash,
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
        candidate_hash=session.current_hash,
        findings=[f.to_json() for f in result.findings],
    )
