"""에이전트 ↔ 런타임 구조화 프로토콜 (PRD §5.1.2).

모델의 자유 텍스트만으로는 도구·예산·계약이 바뀌지 않는다.
여기의 파서는 **런타임이 받아들일 수 있는 것만** 통과시킨다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    """전문 역할의 반환. 최종 제품 판정이 아니다."""

    COMPLETED = "completed"
    NEED_MORE_EVIDENCE = "need_more_evidence"
    BLOCKED = "blocked"
    FAILED = "failed"


class LeadAction(StrEnum):
    """main 이 할 수 있는 것 전부 (PRD §5.3.3)."""

    DELEGATE = "delegate"
    REVISE_PLAN = "revise_plan"
    REQUEST_FINISH = "request_finish"
    REQUEST_STOP = "request_stop"


class ProtocolError(RuntimeError):
    """구조화 반환이 계약을 만족하지 않습니다."""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """모델 출력에서 JSON 객체 하나를 꺼낸다.

    코드펜스·앞뒤 설명을 관대하게 다루되, **객체가 아니면 거절**한다.
    관대함은 형식에만 적용하고 내용에는 적용하지 않는다.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ProtocolError("모델 출력에서 JSON 객체를 찾지 못했습니다.")


@dataclass(frozen=True, slots=True)
class ToolRequest:
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """전문 역할의 한 턴: 도구를 쓰거나 결과를 낸다."""

    tool_request: ToolRequest | None
    result: "AgentResult | None"
    note: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    risk_id: str
    hypothesis: str
    invariant: str
    conclusion: str            # no_issue | issue | inconclusive
    evidence_ids: tuple[str, ...]
    probe_result_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id, "hypothesis": self.hypothesis,
            "invariant": self.invariant, "conclusion": self.conclusion,
            "evidence_ids": list(self.evidence_ids),
            "probe_result_ids": list(self.probe_result_ids),
        }


_CONCLUSIONS = {"no_issue", "issue", "inconclusive"}


@dataclass(frozen=True, slots=True)
class AgentResult:
    """전문 역할의 최종 반환 (PRD §5.1.2 공통 반환 필드)."""

    outcome: Outcome
    summary: str
    findings: tuple[Finding, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    suggested_next_action: str = ""
    artifact_refs: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome), "summary": self.summary,
            "findings": [f.to_json() for f in self.findings],
            "evidence_ids": list(self.evidence_ids),
            "unknowns": list(self.unknowns),
            "suggested_next_action": self.suggested_next_action,
            "artifact_refs": list(self.artifact_refs),
        }


def parse_agent_turn(text: str) -> AgentTurn:
    """전문 역할의 출력을 해석한다."""
    data = parse_json_object(text)

    if "tool" in data:
        tool = str(data["tool"]).strip()
        if not tool:
            raise ProtocolError("tool 이름이 비어 있습니다.")
        args = data.get("args") or {}
        if not isinstance(args, dict):
            raise ProtocolError("args 는 객체여야 합니다.")
        return AgentTurn(
            tool_request=ToolRequest(tool=tool, args=args),
            result=None, note=str(data.get("note", ""))[:400],
        )

    if "outcome" not in data:
        raise ProtocolError("tool 또는 outcome 중 하나가 있어야 합니다.")
    return AgentTurn(tool_request=None, result=parse_agent_result(data))


def parse_agent_result(data: dict[str, Any]) -> AgentResult:
    raw_outcome = str(data.get("outcome", "")).strip()
    try:
        outcome = Outcome(raw_outcome)
    except ValueError as exc:
        raise ProtocolError(
            f"허용되지 않은 outcome: {raw_outcome!r}. "
            f"허용: {sorted(o.value for o in Outcome)}"
        ) from exc

    findings: list[Finding] = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            raise ProtocolError("findings 항목은 객체여야 합니다.")
        conclusion = str(raw.get("conclusion", "")).strip()
        if conclusion not in _CONCLUSIONS:
            raise ProtocolError(
                f"허용되지 않은 conclusion: {conclusion!r}. 허용: {sorted(_CONCLUSIONS)}"
            )
        findings.append(
            Finding(
                risk_id=str(raw.get("risk_id", "")).strip(),
                hypothesis=str(raw.get("hypothesis", ""))[:600],
                invariant=str(raw.get("invariant", ""))[:120],
                conclusion=conclusion,
                evidence_ids=tuple(str(x) for x in (raw.get("evidence_ids") or [])),
                probe_result_ids=tuple(
                    str(x) for x in (raw.get("probe_result_ids") or [])
                ),
            )
        )

    return AgentResult(
        outcome=outcome,
        summary=str(data.get("summary", ""))[:1200],
        findings=tuple(findings),
        evidence_ids=tuple(str(x) for x in (data.get("evidence_ids") or [])),
        unknowns=tuple(str(x)[:300] for x in (data.get("unknowns") or [])),
        suggested_next_action=str(data.get("suggested_next_action", ""))[:400],
        artifact_refs=tuple(str(x) for x in (data.get("artifact_refs") or [])),
    )


@dataclass(frozen=True, slots=True)
class LeadDecision:
    """main 의 한 수."""

    action: LeadAction
    agent_id: str = ""
    objective: str = ""
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""
    stop_reason: str = ""
    plan: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "action": str(self.action), "agent_id": self.agent_id,
            "objective": self.objective, "evidence_ids": list(self.evidence_ids),
            "reason": self.reason, "stop_reason": self.stop_reason,
            "plan": list(self.plan),
        }


DELEGATABLE = (
    "spec_researcher", "runtime_diagnostician", "repair_engineer", "data_auditor",
)


def parse_lead_decision(text: str) -> LeadDecision:
    """main 의 출력을 해석한다.

    위임 대상은 등록된 4개뿐이다. 모델이 다른 이름을 쓰면 거절한다 (AC-01).
    """
    data = parse_json_object(text)
    raw_action = str(data.get("action", "")).strip()
    try:
        action = LeadAction(raw_action)
    except ValueError as exc:
        raise ProtocolError(
            f"허용되지 않은 action: {raw_action!r}. "
            f"허용: {sorted(a.value for a in LeadAction)}"
        ) from exc

    agent_id = str(data.get("agent_id", "")).strip()
    if action is LeadAction.DELEGATE:
        if agent_id not in DELEGATABLE:
            raise ProtocolError(
                f"등록되지 않은 위임 대상: {agent_id!r}. 허용: {list(DELEGATABLE)}"
            )
        if not str(data.get("objective", "")).strip():
            raise ProtocolError("위임에는 objective 가 필요합니다.")

    return LeadDecision(
        action=action,
        agent_id=agent_id,
        objective=str(data.get("objective", ""))[:600],
        evidence_ids=tuple(str(x) for x in (data.get("evidence_ids") or [])),
        reason=str(data.get("reason", ""))[:600],
        stop_reason=str(data.get("stop_reason", "")).strip(),
        plan=tuple(str(x)[:200] for x in (data.get("plan") or [])),
    )
