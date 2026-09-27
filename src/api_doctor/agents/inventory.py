"""tool inventory 실측 검사 (AC-01, PRD §5.3.4).

프롬프트는 셋 중 어느 것도 아니다. 검사 지점은 둘이다:
초기화 직후(여기)와 호출 시점(도구 게이트웨이).
"""

from __future__ import annotations

from ..runtime.evidence import AUDIT, DIAG, MAIN, REPAIR, SPEC
from ..tools.gateway import ACTION_ACL, TOOL_ACL, Action, ToolGateway

# 제품 구조. AC-01 이 요구하는 main 1 + 서브 4.
REGISTERED_AGENTS = (MAIN, SPEC, DIAG, REPAIR, AUDIT)
DELEGATION_TARGETS = (SPEC, DIAG, REPAIR, AUDIT)

# 비교 실험 B 의 대조군 (PRD §7.3). 제품 팀에 속하지 않으며 위임 대상도 아니다.
CONTROL_AGENTS = ("single_agent",)

# 어떤 역할에도 있어서는 안 되는 이름. 프레임워크가 몰래 싣는 것을 잡는다.
FORBIDDEN_TOOL_NAMES = frozenset({
    "execute", "shell", "bash", "run_command",
    "write_file", "edit_file", "delete", "ls", "glob", "grep", "read_file",
    "task", "create_subagent", "spawn",
})


class InventoryViolation(RuntimeError):
    """실제 도구 목록이 등록된 경계와 다릅니다."""


def assert_inventory(gateway: ToolGateway) -> dict[str, list[str]]:
    """초기화 직후 실제 발급되는 도구를 확인한다.

    허용 목록 밖 도구·동적 생성·하위 재위임이 하나라도 보이면 실행하지 않는다.
    """
    problems: list[str] = []

    # 제품 팀은 정확히 5개여야 한다. 대조군은 팀 밖의 별도 주체다.
    team = set(TOOL_ACL) - set(CONTROL_AGENTS)
    if team != set(REGISTERED_AGENTS):
        problems.append(
            f"제품 팀이 정확히 main+4 가 아닙니다: {sorted(team)}"
        )
    for control in CONTROL_AGENTS:
        if control in DELEGATION_TARGETS:
            problems.append(f"대조군이 위임 대상에 들어 있습니다: {control}")
        if ACTION_ACL.get(control):
            problems.append(f"대조군이 하네스 행동을 갖고 있습니다: {control}")

    inventory: dict[str, list[str]] = {}
    for agent_id in REGISTERED_AGENTS:
        tools = gateway.inventory_for(agent_id)
        inventory[agent_id] = tools

        leaked = sorted(set(tools) & FORBIDDEN_TOOL_NAMES)
        if leaked:
            problems.append(f"{agent_id} 에 금지된 도구가 있습니다: {leaked}")

        allowed = {str(t) for t in TOOL_ACL[agent_id]}
        extra = sorted(set(tools) - allowed)
        if extra:
            problems.append(f"{agent_id} 에 허용 목록 밖 도구가 있습니다: {extra}")

    # 위임·계획 행동은 main 만 갖는다. 그래서 하위 재위임이 불가능하다.
    for agent_id in DELEGATION_TARGETS:
        if ACTION_ACL.get(agent_id):
            problems.append(
                f"{agent_id} 가 하네스 행동을 갖고 있습니다: "
                f"{sorted(str(a) for a in ACTION_ACL[agent_id])}"
            )
    if ACTION_ACL.get(MAIN) != {Action.DELEGATE, Action.REVISE_PLAN}:
        problems.append("main 의 행동 목록이 등록과 다릅니다.")

    if problems:
        raise InventoryViolation(
            "도구 경계 검사 실패:\n  " + "\n  ".join(problems)
        )
    return inventory
