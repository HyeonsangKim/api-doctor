"""위임 경계 미들웨어 (FR-003, PRD §5.3.3).

deepagents 의 `task` 는 LangGraph 런타임 주입을 요구해 도구 객체를 밖에서
감쌀 수 없다. 대신 LangChain 의 `wrap_tool_call` 훅으로 **dispatch 직전에**
가로챈다. 이 지점에서 세 가지를 강제한다.

1. 등록된 4개 외 위임 대상 거절
2. 위임 예산 차감 (전체 8, 역할당 2)
3. 같은 역할·같은 질문·같은 후보의 중복 위임 거절 (NO_NEW_EVIDENCE)

`task` 를 감싸지 못해 builtin 하네스에만 있던 검사를 여기서 되살린다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from ..runtime.budget import BudgetExceeded, BudgetLedger
from ..runtime.events import EventStore, EventType
from .lead import DelegationKey, LeadState, _shape


@dataclass(slots=True)
class DelegationRecord:
    """가로챈 위임 1건. 보고서의 역할별 기여가 된다."""

    agent_id: str
    objective: str
    accepted: bool
    reason: str = ""
    returned: str = ""
    tool_calls: list[str] = field(default_factory=list)
    candidate_hash: str = ""

    @property
    def completed(self) -> bool:
        """이 위임이 결론까지 냈는가.

        `need_more_evidence` · `blocked` 는 완료가 아니므로 재위임을 막지 않는다.
        """
        if not self.accepted or not self.returned:
            return False
        try:
            from ..jsonio import parse_json_object

            return str(parse_json_object(self.returned).get("outcome")) == "completed"
        except Exception:  # noqa: BLE001 - 형식이 깨졌으면 완료로 보지 않는다
            return False


class DelegationGuard(AgentMiddleware):
    """`task` 호출을 dispatch 직전에 검사한다."""

    #: 수리·감사를 위해 남겨 두는 모델 호출 수.
    #: 조사 역할이 전체 예산을 먹어 감사가 굶는 것을 막는다.
    RESERVED_FOR_LATE_STAGES = 8
    LATE_STAGES = ("repair_engineer", "data_auditor")

    def __init__(
        self,
        *,
        ledger: BudgetLedger,
        events: EventStore,
        targets: tuple[str, ...],
        current_hash: Callable[[], str],
        inventory: dict[str, list[str]],
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._events = events
        self._targets = set(targets)
        self._current_hash = current_hash
        self._inventory = inventory
        self.state = LeadState()
        self.records: list[DelegationRecord] = []

    # ------------------------------------------------------------------ 훅

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = getattr(request, "tool_call", None) or {}
        if call.get("name") != "task":
            return handler(request)

        args = call.get("args") or {}
        agent_id = str(args.get("subagent_type", "")).strip()
        objective = str(args.get("description", ""))
        call_id = str(call.get("id", "task"))

        refusal = self._check(agent_id, objective)
        if refusal is not None:
            code, message = refusal
            self.records.append(
                DelegationRecord(
                    agent_id=agent_id or "(미지정)", objective=objective,
                    accepted=False, reason=code,
                    candidate_hash=self._current_hash(),
                )
            )
            return ToolMessage(
                content=json.dumps({"error": code, "message": message},
                                   ensure_ascii=False),
                name="task", tool_call_id=call_id, status="error",
            )

        record = DelegationRecord(
            agent_id=agent_id, objective=objective, accepted=True,
            candidate_hash=self._current_hash(),
        )
        self.records.append(record)
        self._events.append(
            EventType.DELEGATION_STARTED,
            f"{agent_id} 에게 위임했습니다: {objective[:100]}",
            agent_id=agent_id, objective=objective,
            candidate_hash=self._current_hash(), harness="deepagents",
            allowed_tools=self._inventory.get(agent_id, []),
        )

        result = handler(request)
        record.returned = _text_of(result)
        return result

    # -------------------------------------------------------------- 검사

    def _check(self, agent_id: str, objective: str) -> tuple[str, str] | None:
        if agent_id not in self._targets:
            return (
                "FORBIDDEN",
                f"등록되지 않은 위임 대상입니다: {agent_id!r}. "
                f"가능한 대상: {sorted(self._targets)}",
            )

        candidate_hash = self._current_hash()

        pending_late = [
            stage for stage in self.LATE_STAGES
            if not any(r.agent_id == stage and r.accepted for r in self.records)
        ]

        # 이미 결론을 받은 역할에 **아직 수리·감사가 남은 동안** 다시 묻지 않는다.
        # 문구만 바꾼 재위임이 낱말 해시 검사를 빠져나가 예산을 태우고,
        # 그 바람에 감사가 굶는 것을 실측에서 확인했다.
        # 수리·감사가 끝난 뒤의 재위임은 적응성이므로 막지 않는다 (AC-03).
        answered = [
            r for r in self.records
            if r.agent_id == agent_id
            and r.candidate_hash == candidate_hash
            and r.completed
        ]
        if answered and pending_late and agent_id not in self.LATE_STAGES:
            self._events.append(
                EventType.DELEGATION_REJECTED,
                f"{agent_id} 는 현재 후보에 대해 이미 결론을 냈습니다.",
                reason="ALREADY_ANSWERED", agent_id=agent_id,
            )
            return (
                "ALREADY_ANSWERED",
                f"{agent_id} 는 현재 후보에 대해 이미 결론을 냈습니다: "
                f"{answered[-1].returned[:200]}\n"
                "같은 질문을 다시 하지 말고 다음 단계로 넘어가세요.",
            )

        # 수리·감사를 위한 호출을 남겨 둔다.
        if agent_id not in self.LATE_STAGES and pending_late:
            remaining = self._ledger.remaining()["model_calls"]
            if remaining <= self.RESERVED_FOR_LATE_STAGES:
                self._events.append(
                    EventType.DELEGATION_REJECTED,
                    f"{agent_id} 위임을 보류했습니다. 남은 호출을 "
                    f"{'·'.join(pending_late)} 에 씁니다.",
                    reason="RESERVED_FOR_LATE_STAGES", agent_id=agent_id,
                )
                return (
                    "RESERVED_FOR_LATE_STAGES",
                    f"남은 호출 {remaining}회는 {', '.join(pending_late)} 를 위해 "
                    "남겨 둡니다. 조사는 여기까지 하고 다음 단계로 넘어가세요.",
                )

        key = DelegationKey(
            agent_id=agent_id, objective_shape=_shape(objective),
            evidence_hash="", candidate_hash=candidate_hash,
        )
        if self.state.seen(key):
            fingerprint = (key.agent_id, key.objective_shape, key.candidate_hash)
            repeated = fingerprint in self.state.rejected_once
            self.state.rejected_once.add(fingerprint)
            self._events.append(
                EventType.DELEGATION_REJECTED,
                f"{agent_id} 에 대한 중복 위임을 거절했습니다.",
                reason="NO_NEW_EVIDENCE", agent_id=agent_id, repeated=repeated,
            )
            return (
                "NO_NEW_EVIDENCE",
                "같은 역할·같은 질문·같은 후보로 다시 위임했습니다. "
                + ("두 번째 반복입니다. 다른 조사를 고르거나 종료하세요."
                   if repeated else "새 근거나 더 구체적인 질문이 필요합니다."),
            )

        # 중복 검사를 상한보다 **먼저** 한다 (PRD §5.3.3).
        try:
            self._ledger.spend_delegation(agent_id)
        except BudgetExceeded as exc:
            self._events.append(
                EventType.DELEGATION_REJECTED, str(exc)[:150],
                reason=str(exc.verdict.reason), agent_id=agent_id,
            )
            return ("BUDGET_EXHAUSTED", str(exc))

        self.state.delegation_history.append(key)
        return None


def _text_of(result: Any) -> str:
    """서브에이전트의 반환 본문을 꺼낸다.

    `task` 는 `ToolMessage` 가 아니라 상태 갱신을 담은 `Command` 를 돌려준다.
    그 안의 마지막 ToolMessage 가 실제 반환이다.
    """
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages") or []
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if content:
                return _flatten(content)
        return ""
    return _flatten(getattr(result, "content", result))


def _flatten(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)
