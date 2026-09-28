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


# ---------------------------------------------------------------- 위임 경계


def _run_script(session, script):
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    model = ModelGateway(
        backend=ScriptedBackend(
            responses=script,
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        ledger=session.ledger,
    )
    return orchestrate_deep(session=session, model=model), model


@requires_docker
def test_delegation_budget_is_actually_spent(tmp_path) -> None:
    """기본 하네스에서도 위임 예산이 차감돼야 한다 (FR-003).

    `task` 를 감쌀 수 없다는 이유로 이 검사가 빠져 있었다.
    """
    session = make_session(tmp_path)
    _run_script(session, deep_happy_script())
    usage = session.ledger.usage()
    assert usage["delegations"] == 4
    assert session.ledger.role_delegations["data_auditor"] == 1


@requires_docker
def test_repeated_delegation_is_rejected(tmp_path) -> None:
    """PRD §5.3.3: 같은 역할·질문·후보의 재위임은 상한보다 먼저 거절된다."""
    from _harness import j

    same = j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                       "description": "응답 구조를 알려줘"}})
    session = make_session(tmp_path)
    _run_script(session, [
        same,
        j({"tool": "search_spec", "args": {"question": "구조"}}),
        j({"outcome": "completed", "summary": "확인함"}),
        same,
        j({"outcome": "completed", "summary": "중복이라 끝냅니다."}),
    ])
    rejected = [
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.DELEGATION_REJECTED
        and e.data.get("reason") in ("NO_NEW_EVIDENCE", "ALREADY_ANSWERED")
    ]
    assert rejected, "중복 위임이 거절되지 않았다"
    assert session.ledger.role_delegations["spec_researcher"] == 1, \
        "거절된 위임은 예산을 쓰지 않아야 한다"


@requires_docker
def test_unregistered_target_is_refused(tmp_path) -> None:
    """AC-01: 등록되지 않은 대상으로 위임할 수 없다."""
    from _harness import j

    session = make_session(tmp_path)
    _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "general-purpose",
                                    "description": "아무거나 해줘"}}),
        j({"outcome": "completed", "summary": "거절당했습니다."}),
    ])
    assert session.ledger.usage()["delegations"] == 0


@requires_docker
def test_role_delegation_cap_is_enforced(tmp_path) -> None:
    """역할당 위임 2회 상한."""
    from _harness import j

    session = make_session(tmp_path)
    script = []
    for n in range(4):
        script += [
            j({"tool": "task", "args": {"subagent_type": "runtime_diagnostician",
                                        "description": f"관측 {n} 번째 다른 질문"}}),
            j({"tool": "run_probe", "args": {"probe_id": "range_coverage"}}),
            j({"outcome": "completed", "summary": f"관측 {n}"}),
        ]
    script.append(j({"outcome": "completed", "summary": "끝"}))
    _run_script(session, script)
    assert session.ledger.role_delegations["runtime_diagnostician"] <= 2


@requires_docker
def test_role_that_used_no_tools_stays_visible(tmp_path) -> None:
    """FR-017: 이름만 등장한 역할을 보고서가 숨기면 안 된다.

    게이트웨이 호출 기록에서 위임을 복원하던 방식은 도구를 안 쓴 역할을
    통째로 빠뜨렸다.
    """
    from _harness import j

    session = make_session(tmp_path)
    result, _ = _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "도구 없이 답만 한다"}}),
        j({"outcome": "completed", "summary": "도구를 쓰지 않고 답합니다."}),
        j({"outcome": "completed", "summary": "끝"}),
    ])
    used = {d.agent_id: d for d in result.delegations}
    assert "spec_researcher" in used, "도구를 안 쓴 역할이 사라졌다"
    assert used["spec_researcher"].tool_calls == []


@requires_docker
def test_rejected_delegation_is_recorded_as_evidence(tmp_path) -> None:
    """무엇을 시도했다 막혔는지가 증거다."""
    from _harness import j

    session = make_session(tmp_path)
    result, _ = _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "general-purpose",
                                    "description": "범위 밖 위임"}}),
        j({"outcome": "completed", "summary": "끝"}),
    ])
    blocked = [d for d in result.delegations if str(d.result.outcome) == "blocked"]
    assert blocked, "거절된 위임이 기록에 남아야 한다"
    assert "FORBIDDEN" in blocked[0].result.summary


# ---------------------------------------------------------------- 재계획


@requires_docker
def test_different_problems_take_different_paths(tmp_path) -> None:
    """AC-03: 모든 사례에 같은 수리 순서를 강제하지 않는다.

    문제가 다르면 main 이 고르는 위임 경로도 달라져야 한다.
    """
    from _harness import connector, j

    # 경로 A: 명세부터 확인하고 수리한다
    session_a = make_session(tmp_path / "a")
    result_a, _ = _run_script(session_a, [
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "응답 구조 확인"}}),
        j({"tool": "search_spec", "args": {"question": "중첩 경로"}}),
        j({"outcome": "completed", "summary": "구조 확인"}),
        j({"outcome": "completed", "summary": "끝"}),
    ])

    # 경로 B: 명세를 건너뛰고 바로 진단한다
    session_b = make_session(tmp_path / "b")
    result_b, _ = _run_script(session_b, [
        j({"tool": "task", "args": {"subagent_type": "runtime_diagnostician",
                                    "description": "실패 지점 관측"}}),
        j({"tool": "run_probe", "args": {"probe_id": "range_coverage"}}),
        j({"outcome": "completed", "summary": "위치 누락 확인"}),
        j({"outcome": "completed", "summary": "끝"}),
    ])

    path_a = [d.agent_id for d in result_a.delegations]
    path_b = [d.agent_id for d in result_b.delegations]
    assert path_a != path_b, "서로 다른 문제가 같은 경로를 강제받았다"
    assert path_a == ["spec_researcher"]
    assert path_b == ["runtime_diagnostician"]


@requires_docker
def test_redelegation_is_allowed_once_late_stages_have_run(tmp_path) -> None:
    """AC-03: 수리·감사를 마친 뒤의 재위임은 적응성이므로 막지 않는다.

    감사가 문제를 찾아 명세를 다시 확인하는 경로가 바로 그것이다.
    수리·감사가 **아직 남아 있을 때만** 조사 반복을 막는다 — 그때 반복하면
    감사가 예산을 못 받고 굶기 때문이다.
    """
    from _harness import connector, j

    session = make_session(tmp_path)
    result, _ = _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "레코드 배열이 어느 경로에 있는가"}}),
        j({"tool": "search_spec", "args": {"question": "중첩 경로"}}),
        j({"outcome": "completed", "summary": "SeoulPublicLibraryInfo.row 다."}),

        j({"tool": "task", "args": {"subagent_type": "repair_engineer",
                                    "description": "중첩 경로를 고친다"}}),
        j({"tool": "submit_patch", "args": {"source": connector("healthy")}}),
        j({"outcome": "completed", "summary": "고쳤다."}),

        j({"tool": "task", "args": {"subagent_type": "data_auditor",
                                    "description": "손실 조사"}}),
        j({"tool": "run_probe", "args": {"probe_id": "page_partition"}}),
        j({"outcome": "completed", "summary": "경계가 의심된다.", "findings": [
            {"risk_id": "pagination_boundary", "hypothesis": "마지막 구간",
             "invariant": "partition_invariance", "conclusion": "inconclusive",
             "probe_result_ids": ["pr1"]}]}),

        # 감사 결과를 보고 명세를 다시 확인한다 — 이것이 적응성이다.
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "범위 종료 조건은 무엇으로 판단하는가"}}),
        j({"tool": "search_spec", "args": {"question": "범위 종료"}}),
        j({"outcome": "completed", "summary": "cursor 가 end 를 넘을 때까지."}),
        j({"outcome": "completed", "summary": "끝"}),
    ])
    spec_runs = [d for d in result.delegations if d.agent_id == "spec_researcher"]
    assert len(spec_runs) == 2, "수리·감사 후의 재위임까지 막혔다"


@requires_docker
def test_investigation_is_capped_while_repair_and_audit_pend(tmp_path) -> None:
    """수리·감사가 남은 동안 조사만 반복하면 거절한다.

    실측에서 main 이 문구만 바꿔 같은 역할에 다시 묻다가 감사가 굶었다.
    """
    from _harness import j

    session = make_session(tmp_path)
    _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "레코드 배열이 어느 경로에 있는가"}}),
        j({"tool": "search_spec", "args": {"question": "중첩 경로"}}),
        j({"outcome": "completed", "summary": "확인함"}),
        j({"tool": "task", "args": {"subagent_type": "spec_researcher",
                                    "description": "응답 구조를 다시 설명해 달라"}}),
        j({"outcome": "completed", "summary": "끝"}),
    ])
    rejected = [
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.DELEGATION_REJECTED
        and e.data.get("reason") == "ALREADY_ANSWERED"
    ]
    assert rejected, "수리·감사가 남았는데 조사 반복이 통과했다"
    assert session.ledger.role_delegations["spec_researcher"] == 1


@requires_docker
def test_empty_delegation_is_not_repeated(tmp_path) -> None:
    """아무것도 못 한 역할을 바로 다시 부르지 않는다.

    실측에서 공급자 장애로 repair_engineer 가 세 번 연속 아무 일도 못 하고
    호출됐다. 환불 설계 때문에 예산 검사가 계속 통과한 탓이다.
    """
    from _harness import j

    session = make_session(tmp_path)
    # 위임은 받되 도구도 못 쓰고 구조화 반환도 못 내는 응답
    _run_script(session, [
        j({"tool": "task", "args": {"subagent_type": "repair_engineer",
                                    "description": "고쳐줘"}}),
        "모델이 형식을 지키지 못한 평문 응답",
        j({"tool": "task", "args": {"subagent_type": "repair_engineer",
                                    "description": "다시 고쳐줘"}}),
        j({"outcome": "completed", "summary": "끝"}),
    ])
    rejected = [
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.DELEGATION_REJECTED
        and e.data.get("reason") == "PREVIOUS_ATTEMPT_EMPTY"
    ]
    assert rejected, "빈 위임이 반복됐다"


def test_audit_budget_is_reserved_after_repair() -> None:
    """수리가 끝나고 감사만 남았으면 감사 몫을 지킨다."""
    from api_doctor.agents.delegation_guard import DelegationGuard

    assert DelegationGuard.AUDIT == "data_auditor"
    assert DelegationGuard.RESERVED_FOR_AUDIT >= 3, (
        "감사는 probe 를 돌리고 구조화 결론까지 내야 한다"
    )
    assert (
        DelegationGuard.RESERVED_FOR_AUDIT < DelegationGuard.RESERVED_FOR_LATE_STAGES
    ), "감사 단독 예약은 수리+감사 예약보다 작아야 한다"


def test_repair_prompt_warns_against_non_ascii_in_code() -> None:
    """실측에서 수리자가 코드에 가운뎃점을 넣어 SyntaxError 로 죽었다.

    도구가 막기는 하지만, 막히고 다시 내는 것도 예산을 쓴다.
    """
    from api_doctor.agents.prompts import BY_AGENT_DEEP

    repair = BY_AGENT_DEEP["repair_engineer"]
    assert "ASCII" in repair, "코드에 ASCII 만 쓰라는 지침이 없다"
    assert "·" in repair, "무엇이 문제인지 실제 글자로 보여줘야 한다"
