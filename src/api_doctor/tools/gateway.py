"""도구 게이트웨이 (FR-002, AC-07, PRD §5.1.3).

**권한은 실행 컨텍스트에서 나온다.** 모델이 인자로 넘기는 `agent_id` 는 무시된다.
도구는 역할별로 바인딩된 클로저로 발급되며, 에이전트는 자기 것이 아닌 도구의
이름조차 알 필요가 없다.

프롬프트는 설명 수단이지 권한 통제 수단이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from ..runtime.budget import BudgetLedger, Verdict
from ..runtime.evidence import MAIN, SPEC, DIAG, REPAIR, AUDIT


class Tool(StrEnum):
    """PRD §5.1.3 의 9종 도구."""

    READ_EVIDENCE = "read_evidence"
    SEARCH_SPEC = "search_spec"
    INSPECT_CODE = "inspect_code"
    INSPECT_TRACE = "inspect_trace"
    RUN_PROBE = "run_probe"
    SUBMIT_PATCH = "submit_patch"
    GET_BUDGET = "get_budget"
    REQUEST_FINISH = "request_finish"
    REQUEST_STOP = "request_stop"


class Action(StrEnum):
    """main 의 하네스 행동. 도구와 다른 레이어다 (아키텍처 §10.1)."""

    DELEGATE = "delegate"
    REVISE_PLAN = "revise_plan"


# 역할별 허용 도구. 이 표 밖의 조합은 실행 시점에 거절된다.
TOOL_ACL: dict[str, frozenset[Tool]] = {
    MAIN: frozenset({
        Tool.READ_EVIDENCE, Tool.GET_BUDGET,
        Tool.REQUEST_FINISH, Tool.REQUEST_STOP,
    }),
    SPEC: frozenset({Tool.SEARCH_SPEC, Tool.READ_EVIDENCE, Tool.GET_BUDGET}),
    DIAG: frozenset({
        Tool.INSPECT_CODE, Tool.INSPECT_TRACE, Tool.RUN_PROBE,
        Tool.READ_EVIDENCE, Tool.GET_BUDGET,
    }),
    REPAIR: frozenset({
        Tool.INSPECT_CODE, Tool.SUBMIT_PATCH, Tool.RUN_PROBE,
        Tool.READ_EVIDENCE, Tool.GET_BUDGET,
    }),
    AUDIT: frozenset({
        Tool.INSPECT_CODE, Tool.INSPECT_TRACE, Tool.RUN_PROBE,
        Tool.READ_EVIDENCE, Tool.GET_BUDGET,
    }),
}

# 비교 실험 B (PRD §7.3): 같은 도구·자료·예산·검증을 가진 단일 에이전트.
# 정보 차이로 팀(C)을 유리하게 만들지 않기 위해 네 역할의 도구를 모두 준다.
SINGLE = "single_agent"
TOOL_ACL[SINGLE] = frozenset({
    Tool.SEARCH_SPEC, Tool.INSPECT_CODE, Tool.INSPECT_TRACE, Tool.RUN_PROBE,
    Tool.SUBMIT_PATCH, Tool.READ_EVIDENCE, Tool.GET_BUDGET,
    Tool.REQUEST_FINISH, Tool.REQUEST_STOP,
})

# 행동은 main 만 가진다. 그래서 하위 재위임이 구조적으로 불가능하다 (AC-01).
ACTION_ACL: dict[str, frozenset[Action]] = {
    MAIN: frozenset({Action.DELEGATE, Action.REVISE_PLAN}),
    SPEC: frozenset(), DIAG: frozenset(),
    REPAIR: frozenset(), AUDIT: frozenset(),
    SINGLE: frozenset(),   # 단일 에이전트는 위임하지 않는다
}


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Lease:
    """위임 1건에 발급되는 예산 임대 (PRD §5.1.2)."""

    model_calls: int
    sandbox_runs: int
    deadline: float

    def to_json(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "sandbox_runs": self.sandbox_runs,
            "deadline": round(self.deadline, 1),
        }


@dataclass(frozen=True, slots=True)
class ToolContext:
    """도구 호출의 **실행 컨텍스트**. 런타임이 발급하며 모델이 만들 수 없다.

    `agent_id` 가 여기 있다는 것이 이 설계의 핵심이다. 모델이 프롬프트나
    인자로 역할을 위조해도 이 값은 바뀌지 않는다 (AC-07).
    """

    run_id: str
    task_id: str
    agent_id: str
    candidate_hash: str
    contract_hash: str
    snapshot_hash: str
    lease: Lease
    allowed_probe_ids: tuple[str, ...] = ()

    def authorize(self, tool: Tool) -> None:
        allowed = TOOL_ACL.get(self.agent_id, frozenset())
        if tool not in allowed:
            raise ToolError(
                "FORBIDDEN",
                f"{self.agent_id} 는 {tool} 를 사용할 수 없습니다. "
                f"허용: {sorted(str(t) for t in allowed)}",
            )

    def authorize_action(self, action: Action) -> None:
        if action not in ACTION_ACL.get(self.agent_id, frozenset()):
            raise ToolError(
                "FORBIDDEN",
                f"{self.agent_id} 는 {action} 를 수행할 수 없습니다. "
                "하위 재위임은 구조적으로 허용되지 않습니다.",
            )


ToolImpl = Callable[..., Any]


@dataclass(slots=True)
class ToolGateway:
    """도구 구현을 보관하고 역할별로 바인딩해 발급한다."""

    ledger: BudgetLedger
    _impls: dict[Tool, ToolImpl] = field(default_factory=dict)
    _log: list[dict[str, Any]] = field(default_factory=list)
    on_tool_call: Callable[[dict[str, Any]], None] | None = None

    def register(self, tool: Tool, impl: ToolImpl) -> None:
        self._impls[tool] = impl

    def bind(self, context: ToolContext) -> dict[str, ToolImpl]:
        """이 컨텍스트가 쓸 수 있는 도구만 돌려준다.

        반환된 함수는 `context` 를 닫아 갖고 있으므로 호출자가 역할을
        바꿔 넣을 방법이 없다.
        """
        bound: dict[str, ToolImpl] = {}
        for tool in sorted(TOOL_ACL.get(context.agent_id, frozenset())):
            impl = self._impls.get(tool)
            if impl is None:
                continue
            bound[str(tool)] = self._wrap(tool, impl, context)
        return bound

    def _wrap(self, tool: Tool, impl: ToolImpl, context: ToolContext) -> ToolImpl:
        def invoke(**kwargs: Any) -> Any:
            started_ms = int(self.ledger.elapsed * 1000)
            # 모델이 컨텍스트 필드를 인자로 밀어 넣어도 무시한다.
            for reserved in ("agent_id", "run_id", "task_id", "_context"):
                kwargs.pop(reserved, None)

            self.ledger.cancel.raise_if_cancelled()
            try:
                context.authorize(tool)
                result = impl(context, **kwargs)
            except ToolError as exc:
                self._record(tool, context, allowed=False, reason=exc.code,
                             started_ms=started_ms)
                raise
            except TypeError as exc:
                self._record(tool, context, allowed=False, reason="INVALID_ARGS",
                             started_ms=started_ms)
                raise ToolError("INVALID_ARGS", f"{tool} 인자 오류: {exc}") from exc
            self._record(tool, context, allowed=True, reason=None,
                         started_ms=started_ms)
            return result

        invoke.__name__ = str(tool)
        invoke.__doc__ = impl.__doc__
        return invoke

    def _record(
        self, tool: Tool, context: ToolContext, *, allowed: bool,
        reason: str | None, started_ms: int = 0
    ) -> None:
        entry = {
            "tool": str(tool),
            "agent_id": context.agent_id,
            "task_id": context.task_id,
            "candidate_hash": context.candidate_hash,
            "allowed": allowed,
            "reason": reason,
            "started_ms": started_ms,
            "ended_ms": int(self.ledger.elapsed * 1000),
        }
        self._log.append(entry)
        if self.on_tool_call is not None:
            self.on_tool_call(entry)

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._log)

    def inventory_for(self, agent_id: str) -> list[str]:
        """해당 역할에게 실제로 발급되는 도구 이름. AC-01 검사에 쓴다."""
        return sorted(
            str(t) for t in TOOL_ACL.get(agent_id, frozenset()) if t in self._impls
        )


def make_lease(ledger: BudgetLedger, agent_id: str) -> Lease:
    """현재 남은 전체·역할별·시간 예산의 **작은 값**을 임대한다.

    하나를 끝내도 예산이 재설정되지 않는다 (PRD §4.1).
    """
    remaining = ledger.remaining()
    role_left = remaining["role_calls"].get(agent_id, 0)
    return Lease(
        model_calls=max(0, min(role_left, remaining["model_calls"], 4)),
        sandbox_runs=max(0, min(remaining["sandbox_runs"], 2)),
        deadline=ledger.usable_seconds,
    )
