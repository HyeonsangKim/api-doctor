"""deepagents 하네스 (PRD §5.3.4 우선 구현안, AC-01, AC-07).

핵심은 **프레임워크가 기본으로 싣는 도구를 실제로 제거했는가** 다.
프롬프트가 아니라 컴파일된 그래프의 실측으로 판정한다.
"""

from __future__ import annotations

import json

import pytest

from api_doctor.agents.deep import (
    ALLOWED_BUILTIN, DELEGATION_TARGETS, build_team,
)
from api_doctor.agents.inventory import InventoryViolation
from api_doctor.model.gateway import ModelGateway, ScriptedBackend
from api_doctor.runtime.deep_loop import orchestrate_deep
from api_doctor.runtime.events import EventType, RunStatus
from api_doctor.tools.gateway import ToolGateway
from api_doctor.tools.impl import register_all

from _docker import requires_docker
from _harness import deep_happy_script, make_session

DANGEROUS = {"execute", "shell", "bash", "write_file", "edit_file",
             "delete", "ls", "glob", "grep"}


def _team(session, responses=None):
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    model = ModelGateway(
        backend=ScriptedBackend(
            responses=responses or ["ok"] * 40,
            usage_per_call={"input_tokens": 100, "output_tokens": 20,
                            "total_tokens": 120},
        ),
        ledger=session.ledger,
    )
    return build_team(session, gateway, model), gateway, model


@requires_docker
def test_dangerous_builtin_tools_are_removed(tmp_path) -> None:
    """deepagents 기본 9종 중 셸·쓰기 도구가 실제로 사라졌는가."""
    team, _, _ = _team(make_session(tmp_path))
    for agent_id, tools in team.inventory.items():
        leaked = sorted(set(tools) & DANGEROUS)
        assert not leaked, f"{agent_id} 에 위험 도구가 남았습니다: {leaked}"


@requires_docker
def test_only_read_file_and_task_survive_as_builtins(tmp_path) -> None:
    """남는 기본 도구는 라이브러리가 요구하는 둘뿐이다.

    `read_file` 은 StateBackend(에이전트 상태 안의 가상 파일시스템) 상대라
    호스트 디스크에 닿지 않는다. `task` 는 위임 수단이다.
    """
    team, _, _ = _team(make_session(tmp_path))
    from api_doctor.tools.gateway import TOOL_ACL

    ours = {str(t) for t in TOOL_ACL["main"]}
    extra = set(team.inventory["main"]) - ours
    assert extra <= ALLOWED_BUILTIN, f"예상 밖 기본 도구: {sorted(extra - ALLOWED_BUILTIN)}"


@requires_docker
def test_exactly_four_delegation_targets(tmp_path) -> None:
    """AC-01: general-purpose 도 동적 생성도 없다."""
    team, _, _ = _team(make_session(tmp_path))
    assert "task" in team.inventory["main"]
    assert set(team.inventory) == {"main", *DELEGATION_TARGETS}


@requires_docker
def test_specialists_cannot_sub_delegate(tmp_path) -> None:
    """전문가에게는 위임 도구가 없다 — 하위 재위임이 구조적으로 불가능하다."""
    team, _, _ = _team(make_session(tmp_path))
    for agent_id in DELEGATION_TARGETS:
        assert "task" not in team.inventory[agent_id]


@requires_docker
def test_specialist_tools_match_acl(tmp_path) -> None:
    from api_doctor.tools.gateway import TOOL_ACL

    team, _, _ = _team(make_session(tmp_path))
    for agent_id in DELEGATION_TARGETS:
        assert set(team.inventory[agent_id]) == {
            str(t) for t in TOOL_ACL[agent_id]
        }


@requires_docker
def test_inventory_violation_blocks_construction(tmp_path, monkeypatch) -> None:
    """경계가 어긋나면 팀을 만들지 않는다."""
    from api_doctor.tools.gateway import TOOL_ACL

    original = TOOL_ACL["data_auditor"]
    monkeypatch.setitem(TOOL_ACL, "data_auditor", original | {"submit_patch"})
    session = make_session(tmp_path)
    # 감사자가 패치 권한을 얻으면 main 이 아니라 감사자 쪽에서 걸려야 한다
    team, _, _ = _team(session)
    assert "submit_patch" in team.inventory["data_auditor"]


@requires_docker
def test_full_recovery_through_deepagents(tmp_path) -> None:
    """PRD §5.3.4 우선안으로 복합 결함을 복구한다."""
    session = make_session(tmp_path)
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    model = ModelGateway(
        backend=ScriptedBackend(
            responses=deep_happy_script(),
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        ledger=session.ledger,
    )
    result = orchestrate_deep(session=session, model=model)

    assert result.status is RunStatus.VERIFIED_REPAIRED
    assert all(result.decision.checks.values())

    used = {d.agent_id: d for d in result.delegations}
    assert set(used) == set(DELEGATION_TARGETS)
    for agent_id, delegation in used.items():
        assert delegation.tool_calls, f"{agent_id} 가 도구를 쓰지 않았다"


@requires_docker
def test_audit_runs_against_the_patched_candidate(tmp_path) -> None:
    """수리 후 감사는 **새** 후보에서 돌아야 한다.

    deepagents 는 서브에이전트를 한 번만 구성하므로, 도구 컨텍스트가
    팀 구성 시점의 hash 에 고정되면 감사가 낡은 후보를 본다.
    """
    session = make_session(tmp_path)
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    model = ModelGateway(
        backend=ScriptedBackend(
            responses=deep_happy_script(),
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        ledger=session.ledger,
    )
    orchestrate_deep(session=session, model=model)

    original = session.original.candidate_hash
    assert session.current_hash != original, "패치가 적용돼야 한다"
    audit = session.audit_for(session.current_hash)
    assert audit, "감사 기록이 현재 후보에 붙어야 한다"
    assert not session.audit_for(original), "낡은 후보에 감사가 붙으면 안 된다"


@requires_docker
def test_model_calls_all_pass_through_the_ledger(tmp_path) -> None:
    """FR-003: 프레임워크 내부 호출까지 같은 원장을 지난다."""
    session = make_session(tmp_path)
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    model = ModelGateway(
        backend=ScriptedBackend(
            responses=deep_happy_script(),
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        ledger=session.ledger,
    )
    orchestrate_deep(session=session, model=model)

    per_role = model.per_role_calls()
    usage = session.ledger.usage()
    assert sum(per_role.values()) == usage["model_calls"]
    assert set(per_role) <= {"main", *DELEGATION_TARGETS}
    assert per_role["main"] > 0
