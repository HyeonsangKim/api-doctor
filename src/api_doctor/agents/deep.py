"""deepagents 하네스 (PRD §5.3.4 우선 구현안).

main 은 Deep Agents 의 계획·위임 하네스를 쓰고, 네 전문가는 `subagents` 로
등록된 제한된 agent loop 다. `task` 도구가 위임 수단이며 등록된 4개 외에는
부를 수 없다 (AC-01).

**경계를 만드는 세 가지 설정** (2026-09-27 실측으로 확정):

1. `FilesystemMiddleware(tools=["read_file"])`
   기본 9종 중 `execute`(셸)·`write_file`·`edit_file`·`delete`·`ls`·`glob`·
   `grep` 를 **도구 노드에서 완전히 제거**한다. 스키마에서 숨기는 게 아니라
   dispatch 대상에서 빠진다. `read_file` 은 라이브러리가 요구해 남기지만
   기본 backend 가 `StateBackend`(에이전트 상태 안의 가상 파일시스템)라
   호스트 디스크에 닿지 않는다.

2. `GeneralPurposeSubagentProfile(enabled=False)`
   기본 general-purpose 하위 에이전트를 끈다. `task` 의 대상 목록에서
   실제로 사라지는 것을 확인했다.

3. 도구는 전부 `ToolGateway` 가 바인딩한 클로저다.
   권한은 프롬프트가 아니라 실행 컨텍스트에서 나오며, 모델이 `agent_id` 를
   위조해도 무시된다 (AC-07).

모델은 `GatewayChatModel` 이라 프레임워크 내부 호출도 전부 원장을 지난다 (FR-003).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, create_model

from ..model.chat import for_agent
from ..model.gateway import ModelGateway
from ..runtime.envelope import build_context, build_envelope
from ..runtime.session import RunSession
from ..tools.gateway import TOOL_ACL, Tool, ToolContext, ToolError, ToolGateway
from .delegation_guard import DelegationGuard
from .prompts import BY_AGENT

DELEGATION_TARGETS = (
    "spec_researcher", "runtime_diagnostician", "repair_engineer", "data_auditor",
)
# 라이브러리가 요구해 남는 유일한 기본 도구. StateBackend 상대라 호스트와 무관하다.
ALLOWED_BUILTIN = frozenset({"read_file", "task"})


class HarnessUnavailable(RuntimeError):
    """deepagents 하네스를 구성할 수 없습니다."""


@dataclass(slots=True)
class DeepTeam:
    """구성된 1+4 팀."""

    agent: Any
    inventory: dict[str, list[str]]
    contexts: dict[str, ToolContext] = field(default_factory=dict)
    objectives: dict[str, str] = field(default_factory=dict)
    guard: Any = None


def _describe(tool: Tool) -> str:
    return {
        Tool.READ_EVIDENCE: "허용된 근거 1건의 발췌·출처·hash 를 반환한다.",
        Tool.SEARCH_SPEC: "등록된 공식 자료에서 질문에 관련된 절을 찾는다.",
        Tool.INSPECT_CODE: "원본 또는 현재 후보의 코드를 읽기 전용으로 반환한다.",
        Tool.INSPECT_TRACE: "정제된 실행·요청 기록을 반환한다.",
        Tool.RUN_PROBE: "등록 probe 를 현재 후보에서 실행하고 관측을 반환한다.",
        Tool.SUBMIT_PATCH: "후보 파일의 새 버전을 만든다.",
        Tool.GET_BUDGET: "전체·역할 잔여 예산을 반환한다.",
        Tool.REQUEST_FINISH: "고정 종료 게이트를 예약한다. 성공을 확정하지 않는다.",
        Tool.REQUEST_STOP: "비성공 종료만 확정한다.",
    }[tool]


# 도구별 인자 스키마. `**kwargs` 만으로는 LangChain 이 스키마를 만들지 못해
# 모델이 보낸 인자가 그대로 버려진다 (INVALID_ARGS).
_ARG_SPECS: dict[Tool, dict[str, tuple[Any, Any]]] = {
    Tool.READ_EVIDENCE: {"evidence_id": (str, ...)},
    Tool.SEARCH_SPEC: {"question": (str, ...)},
    Tool.INSPECT_CODE: {"target": (str, "candidate")},
    Tool.INSPECT_TRACE: {},
    Tool.RUN_PROBE: {"probe_id": (str, ...)},
    Tool.SUBMIT_PATCH: {"source": (str, ...), "rationale": (str, "")},
    Tool.GET_BUDGET: {},
    Tool.REQUEST_FINISH: {"reason": (str, "")},
    Tool.REQUEST_STOP: {
        "reason": (str, ...), "evidence_ids": (list[str], None),
    },
}


def _args_schema(tool: Tool) -> type[BaseModel]:
    fields = _ARG_SPECS[tool]
    return create_model(
        f"{tool.value.title().replace('_', '')}Args",
        **{name: (annotation, default) for name, (annotation, default) in fields.items()},
    )


def _as_langchain_tools(
    gateway: ToolGateway,
    context_factory: Callable[[], ToolContext],
) -> list[StructuredTool]:
    """게이트웨이가 바인딩한 클로저를 LangChain 도구로 감싼다.

    컨텍스트를 **호출 시점마다 새로 만든다.** deepagents 는 서브에이전트를
    한 번만 구성하므로, 팀 구성 시점의 candidate_hash 를 고정해 두면
    수리 후 감사가 낡은 후보에서 돌아 STALE 이 된다.

    역할은 factory 에 닫혀 있으므로 호출자가 바꿔 넣을 수 없다 (AC-07).
    """
    probe = context_factory()
    names = sorted(gateway.bind(probe))

    tools: list[StructuredTool] = []
    for name in names:
        tool = Tool(name)

        def make(tool_name: str):
            def call(**kwargs: Any) -> str:
                # 모델이 비운 선택 인자는 구현의 기본값을 쓰게 한다.
                cleaned = {k: v for k, v in kwargs.items() if v is not None}
                impl = gateway.bind(context_factory())[tool_name]
                try:
                    return json.dumps(impl(**cleaned), ensure_ascii=False)[:6000]
                except ToolError as exc:
                    return json.dumps(
                        {"error": exc.code, "message": str(exc)}, ensure_ascii=False
                    )
            return call

        tools.append(
            StructuredTool.from_function(
                func=make(name), name=name,
                description=_describe(tool), args_schema=_args_schema(tool),
            )
        )
    return tools


def build_team(
    session: RunSession, gateway: ToolGateway, model: ModelGateway
) -> DeepTeam:
    """main + 4 전문가를 deepagents 로 구성한다."""
    try:
        from deepagents import (
            FilesystemMiddleware, GeneralPurposeSubagentProfile, HarnessProfile,
            create_deep_agent,
        )
        from deepagents._models import get_model_provider
        from deepagents.profiles import register_harness_profile
    except ImportError as exc:  # pragma: no cover - 선택 의존성
        raise HarnessUnavailable(
            "deepagents 가 설치되지 않았습니다. `uv sync --all-extras` 를 실행하세요."
        ) from exc

    lead_model = for_agent(model, "main")

    # 기본 general-purpose 하위 에이전트를 끈다.
    register_harness_profile(
        get_model_provider(lead_model) or "api-doctor-gateway",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
        ),
    )

    def factory_for(agent_id: str) -> Callable[[], ToolContext]:
        def make() -> ToolContext:
            envelope = build_envelope(
                session, agent_id=agent_id, objective="(런타임 발급)"
            )
            return build_context(session, envelope)

        return make

    contexts: dict[str, ToolContext] = {}
    subagents: list[dict[str, Any]] = []
    for agent_id in DELEGATION_TARGETS:
        factory = factory_for(agent_id)
        contexts[agent_id] = factory()
        subagents.append({
            "name": agent_id,
            "description": _ROLE_SUMMARY[agent_id],
            "system_prompt": BY_AGENT[agent_id],
            "tools": _as_langchain_tools(gateway, factory),
            "model": for_agent(model, agent_id),
        })

    main_factory = factory_for("main")
    contexts["main"] = main_factory()

    inventory_preview = {
        sub["name"]: sorted(t.name for t in sub["tools"]) for sub in subagents
    }
    guard = DelegationGuard(
        ledger=session.ledger, events=session.events,
        targets=DELEGATION_TARGETS,
        current_hash=lambda: session.current_hash,
        inventory=inventory_preview,
    )

    agent = create_deep_agent(
        model=lead_model,
        tools=_as_langchain_tools(gateway, main_factory),
        subagents=subagents,
        system_prompt=BY_AGENT["main"],
        middleware=[FilesystemMiddleware(tools=["read_file"]), guard],
    )

    inventory = assert_deep_inventory(agent, subagents)
    return DeepTeam(agent=agent, inventory=inventory, contexts=contexts, guard=guard)


_ROLE_SUMMARY = {
    "spec_researcher": "공식 명세에서 응답 구조·조회 규칙·오류 구별을 찾는다. 코드를 고치지 않는다.",
    "runtime_diagnostician": "어디서 왜 실패·손실이 생기는지 관측으로 좁힌다. 코드를 고치지 않는다.",
    "repair_engineer": "확인된 원인에 맞춰 최소 패치를 만든다. 이 역할만 코드를 바꾼다.",
    "data_auditor": "수리 설명 없이 독립적으로 데이터 손실을 조사한다. 성공을 확정하지 않는다.",
}


def assert_deep_inventory(agent: Any, subagents: list[dict[str, Any]]) -> dict[str, list[str]]:
    """컴파일된 그래프의 **실제** 도구를 확인한다 (AC-01).

    프롬프트가 아니라 런타임 실측으로 판정한다.
    """
    from .inventory import FORBIDDEN_TOOL_NAMES, InventoryViolation

    node = agent.nodes.get("tools")
    inner = getattr(node, "bound", node)
    main_tools = sorted(getattr(inner, "tools_by_name", {}) or {})

    problems: list[str] = []
    leaked = sorted(set(main_tools) & (FORBIDDEN_TOOL_NAMES - ALLOWED_BUILTIN))
    if leaked:
        problems.append(f"main 에 금지된 도구가 있습니다: {leaked}")

    allowed_main = {str(t) for t in TOOL_ACL["main"]} | ALLOWED_BUILTIN
    extra = sorted(set(main_tools) - allowed_main)
    if extra:
        problems.append(f"main 에 허용 목록 밖 도구가 있습니다: {extra}")

    if "task" not in main_tools:
        problems.append("위임 도구(task)가 없어 전문가를 부를 수 없습니다.")

    # task 가 실제로 나열하는 대상이 정확히 4개인지 본다.
    task_tool = (getattr(inner, "tools_by_name", {}) or {}).get("task")
    if task_tool is not None:
        import re

        listed = re.findall(r"^\s*-\s+([a-z_\-]+):", task_tool.description or "", re.M)
        if sorted(listed) != sorted(DELEGATION_TARGETS):
            problems.append(
                f"위임 대상이 등록과 다릅니다: {listed} != {list(DELEGATION_TARGETS)}"
            )

    inventory = {"main": main_tools}
    for sub in subagents:
        names = sorted(t.name for t in sub["tools"])
        inventory[sub["name"]] = names
        allowed = {str(t) for t in TOOL_ACL[sub["name"]]}
        wrong = sorted(set(names) - allowed)
        if wrong:
            problems.append(f"{sub['name']} 에 허용 밖 도구가 있습니다: {wrong}")
        if "task" in names:
            problems.append(f"{sub['name']} 가 위임 도구를 갖고 있습니다 (하위 재위임).")

    if problems:
        raise InventoryViolation("도구 경계 검사 실패:\n  " + "\n  ".join(problems))
    return inventory
