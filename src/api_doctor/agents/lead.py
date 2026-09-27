"""메인 에이전트 (FR-007, AC-03, PRD §5.3.3).

main 은 LLM 으로 담당자·질문·재계획·종료를 정한다. 코드를 쓰거나 실행하거나
성공을 확정할 수 없다 — 그런 도구가 발급되지 않는다.

`build_lead()` 하나만 바꾸면 하네스를 교체할 수 있도록 격리한다
(아키텍처 §10.1). deepagents 는 도구 경계를 만족하지 못해 기각했다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..model.gateway import ModelGateway
from ..runtime.budget import BudgetLedger
from ..runtime.events import EventType
from ..runtime.session import RunSession
from .protocol import LeadDecision, ProtocolError, parse_lead_decision
from .prompts import RECOVERY_LEAD


@dataclass(frozen=True, slots=True)
class DelegationKey:
    """중복 위임 탐지용 지문 (PRD §5.3.3)."""

    agent_id: str
    objective_shape: str
    evidence_hash: str
    candidate_hash: str


def _shape(objective: str) -> str:
    """문장을 낱말 집합으로 눌러, 표현만 바꾼 재위임을 같은 것으로 본다."""
    words = sorted({w.strip(".,!?·") for w in objective.lower().split() if len(w) > 1})
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


@dataclass(slots=True)
class LeadState:
    """main 이 유지하는 정보 (PRD §5.3.3)."""

    plan: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    delegation_history: list[DelegationKey] = field(default_factory=list)
    rejected_once: set[tuple] = field(default_factory=set)

    def seen(self, key: DelegationKey) -> bool:
        return key in self.delegation_history


class NoNewEvidence(RuntimeError):
    """NO_NEW_EVIDENCE — 새 근거나 구체적 질문 없이 같은 위임을 반복했습니다."""


@dataclass(slots=True)
class RecoveryLead:
    """main 의 한 수를 만든다."""

    session: RunSession
    model: ModelGateway
    state: LeadState = field(default_factory=LeadState)

    def decide(self, observations: list[str]) -> LeadDecision:
        """현재 상황을 보고 다음 행동을 고른다."""
        messages = [
            {"role": "system", "content": RECOVERY_LEAD},
            {"role": "user", "content": self._situation(observations)},
        ]
        reply = self.model.complete(agent_id="main", messages=messages)
        try:
            return parse_lead_decision(reply)
        except ProtocolError as exc:
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": f"출력이 올바르지 않습니다: {exc}\nJSON 객체 하나만 출력하세요.",
            })
            retry = self.model.complete(agent_id="main", messages=messages)
            return parse_lead_decision(retry)

    # ------------------------------------------------------------ 중복 검사

    def check_new_evidence(self, decision: LeadDecision) -> DelegationKey:
        """같은 요청의 반복을 역할별 상한보다 **먼저** 거절한다.

        거절 1회 후에도 같은 요청이면 판정 보류로 끝낸다 (PRD §5.3.3).
        """
        evidence_hash = hashlib.sha256(
            ",".join(sorted(decision.evidence_ids)).encode()
        ).hexdigest()[:16]
        key = DelegationKey(
            agent_id=decision.agent_id,
            objective_shape=_shape(decision.objective),
            evidence_hash=evidence_hash,
            candidate_hash=self.session.current_hash,
        )
        if self.state.seen(key):
            fingerprint = (key.agent_id, key.objective_shape, key.candidate_hash)
            already_warned = fingerprint in self.state.rejected_once
            self.session.events.append(
                EventType.DELEGATION_REJECTED,
                f"{decision.agent_id} 에 대한 중복 위임을 거절했습니다.",
                reason="NO_NEW_EVIDENCE", agent_id=decision.agent_id,
                repeated=already_warned,
            )
            self.state.rejected_once.add(fingerprint)
            raise NoNewEvidence(
                "같은 역할·같은 질문·같은 근거·같은 후보로 다시 위임했습니다. "
                + ("두 번째 반복이라 조사를 종료합니다."
                   if already_warned else "새 근거나 더 구체적인 질문이 필요합니다.")
            )
        self.state.delegation_history.append(key)
        return key

    def repeated_rejection(self, decision: LeadDecision) -> bool:
        fingerprint = (
            decision.agent_id, _shape(decision.objective), self.session.current_hash
        )
        return fingerprint in self.state.rejected_once

    # ------------------------------------------------------------ 상황 요약

    def _situation(self, observations: list[str]) -> str:
        """main 에게 전달되는 요약.

        전체 도구 응답은 하위 컨텍스트와 evidence store 에 남기고
        필요한 요약만 올린다 (PRD §5.3.3).
        """
        remaining = self.session.ledger.remaining()
        payload = {
            "run_id": self.session.run_id,
            "dataset": self.session.dataset.dataset_id,
            "contract": self.session.dataset.contract.neutral_summary(),
            "current_candidate": {
                "hash": self.session.current_hash[:23] + "…",
                "version": self.session.current.version,
            },
            "plan": self.state.plan,
            "confirmed": self.state.confirmed[-8:],
            "open_questions": self.state.open_questions[-6:],
            "observations_so_far": observations[-10:],
            "audit_status": {
                "required_risks": [
                    a.risk_id for a in self.session.dataset.contract.audit_requirements
                ],
                "recorded_for_current": [
                    {"risk_id": r.risk_id, "conclusion": r.conclusion}
                    for r in self.session.audit_for(self.session.current_hash)
                ],
            },
            "budget_remaining": remaining,
            "delegations_used": [
                {"agent_id": k.agent_id} for k in self.state.delegation_history
            ],
        }
        return (
            "현재 상황입니다. 다음 행동 하나를 JSON 으로 결정하세요.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


def build_lead(session: RunSession, model: ModelGateway) -> RecoveryLead:
    """main 하네스의 유일한 생성 지점.

    하네스를 바꾸려면 여기만 바꾼다. 어떤 구현이든 1+4 구조·도구 계약·
    LangGraph 상태를 유지해야 하며, 단일 에이전트로 축소하지 않는다.
    """
    return RecoveryLead(session=session, model=model)
