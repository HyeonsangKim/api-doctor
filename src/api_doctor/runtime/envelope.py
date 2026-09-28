"""task envelope 생성 (PRD §5.1.2, R-07, AC-04).

감사자 envelope 는 main 의 대화나 수리자의 설명을 **상속하지 않는다.**
런타임이 중립 템플릿으로 새로 조립한다. 이것이 "독립 감사"의 실체다.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from ..tools.gateway import Lease, ToolContext, make_lease
from .budget import BudgetLedger
from .evidence import AUDIT, DIAG, MAIN, REPAIR, SPEC, EvidenceStore
from .session import RunSession

# 감사자가 고를 수 있는 probe 는 런타임이 정한다. 모델이 넓히지 못한다.
_AUDIT_PROBES = ("page_partition", "range_coverage", "field_presence")


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    """전문 역할에게 전달되는 작업 지시."""

    task_id: str
    agent_id: str
    objective: str
    run_id: str
    candidate_hash: str
    contract_id: str
    contract_summary: dict[str, Any]
    evidence_ids: tuple[str, ...]
    allowed_probe_ids: tuple[str, ...]
    lease: Lease
    context_notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "objective": self.objective,
            "run_id": self.run_id,
            "candidate_hash": self.candidate_hash,
            "contract_id": self.contract_id,
            "contract": self.contract_summary,
            "evidence_ids": list(self.evidence_ids),
            "allowed_probe_ids": list(self.allowed_probe_ids),
            "budget_lease": self.lease.to_json(),
            "notes": list(self.context_notes),
        }


_AUDIT_TEMPLATE = (
    "현재 후보가 계약의 조회 범위에서 데이터를 잃을 가능성을 조사한다. "
    "각 위험 영역마다 가설을 세우고, 허용된 probe 를 실제로 실행해 관측하고, "
    "결론을 no_issue / issue / inconclusive 로 낸다."
)


def build_envelope(
    session: RunSession,
    *,
    agent_id: str,
    objective: str,
    evidence_ids: tuple[str, ...] = (),
) -> TaskEnvelope:
    """역할에 허용된 자료만 담아 envelope 를 조립한다.

    `task_id` · `agent_id` · hash · lease 는 런타임이 **발급**한다.
    모델은 objective 와 근거 참조를 제안할 뿐이다.
    """
    task_id = f"task_{secrets.token_hex(4)}"
    lease = make_lease(session.ledger, agent_id)
    contract = session.dataset.contract

    if agent_id == AUDIT:
        # 감사자는 수리자의 설명도 main 의 대화도 받지 않는다.
        # objective 조차 고정 템플릿으로 다시 쓴다 (R-07).
        visible = _visible_observations(session.evidence, AUDIT, session.current_hash)
        return TaskEnvelope(
            task_id=task_id, agent_id=AUDIT, objective=_AUDIT_TEMPLATE,
            run_id=session.run_id, candidate_hash=session.current_hash,
            contract_id=contract.contract_id,
            contract_summary=contract.neutral_summary(),
            evidence_ids=visible,
            allowed_probe_ids=_AUDIT_PROBES,
            lease=lease,
            context_notes=(
                "수리 과정의 설명은 제공되지 않는다. 코드와 관측만 보고 판단한다.",
                "각 필수 위험 영역에 대해 probe 를 실제로 실행해야 감사가 완료된다.",
            ),
        )

    allowed = _filter_visible(session.evidence, agent_id, evidence_ids, session.current_hash)
    notes: tuple[str, ...] = ()
    probes: tuple[str, ...] = ()
    if agent_id == DIAG:
        probes = tuple(p.probe_id for p in session.dataset.probes.probes)
        notes = ("원본 재현과 요청 변형으로 원인을 좁힌다. 후보를 고치지 않는다.",)
    elif agent_id == REPAIR:
        probes = tuple(p.probe_id for p in session.dataset.probes.probes)
        notes = ("지정된 후보 파일만 바꾼다. 계약과 기대값은 바꿀 수 없다.",)
    elif agent_id == SPEC:
        notes = ("등록된 스킬 자료만 사용한다. 원격 문서를 새로 가져오지 않는다.",)

    return TaskEnvelope(
        task_id=task_id, agent_id=agent_id, objective=objective,
        run_id=session.run_id, candidate_hash=session.current_hash,
        contract_id=contract.contract_id,
        contract_summary=contract.neutral_summary(),
        evidence_ids=allowed, allowed_probe_ids=probes, lease=lease,
        context_notes=notes,
    )


def build_context(session: RunSession, envelope: TaskEnvelope) -> ToolContext:
    """envelope 에서 실행 컨텍스트를 만든다. 권한의 출처는 여기다."""
    return ToolContext(
        run_id=session.run_id,
        task_id=envelope.task_id,
        agent_id=envelope.agent_id,
        candidate_hash=envelope.candidate_hash,
        contract_hash=session.dataset.contract.contract_hash,
        snapshot_hash=session.snapshot.snapshot_hash,
        lease=envelope.lease,
        allowed_probe_ids=envelope.allowed_probe_ids,
    )


def _visible_observations(
    store: EvidenceStore, agent_id: str, candidate_hash: str
) -> tuple[str, ...]:
    """해당 역할이 볼 수 있는 **관측**만. 주장은 넘기지 않는다."""
    from .evidence import EvidenceKind

    return tuple(
        e.evidence_id
        for e in store.visible_to(agent_id, candidate_hash)
        if e.kind is EvidenceKind.OBSERVATION
    )


def _filter_visible(
    store: EvidenceStore,
    agent_id: str,
    requested: tuple[str, ...],
    candidate_hash: str,
) -> tuple[str, ...]:
    """main 이 참조를 제안해도 상대 역할에 허용된 것만 남는다."""
    allowed = {e.evidence_id for e in store.visible_to(agent_id, candidate_hash)}
    return tuple(e for e in requested if e in allowed)
