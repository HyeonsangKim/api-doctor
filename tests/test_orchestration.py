"""1+4 복구 루프 (FR-007~013, AC-01~04, AC-07, AC-09).

모델은 scripted 다. 여기서 검증하는 것은 **구조**이지 모델의 능력이 아니다.
"""

from __future__ import annotations

import pytest

from api_doctor.agents.inventory import (
    DELEGATION_TARGETS, REGISTERED_AGENTS, InventoryViolation, assert_inventory,
)
from api_doctor.agents.lead import NoNewEvidence, build_lead
from api_doctor.agents.protocol import ProtocolError, parse_lead_decision
from api_doctor.runtime.events import EventStore, EventType
from api_doctor.runtime.gate import GateRejection
from api_doctor.runtime.orchestrator import orchestrate
from api_doctor.runtime.events import RunStatus
from api_doctor.runtime.budget import BudgetLedger
from api_doctor.tools.gateway import Tool, ToolGateway

from _docker import requires_docker
from _harness import connector, happy_path_script, j, make_model, make_session


@requires_docker
def test_full_team_recovers_composite_defect(tmp_path) -> None:
    """네 역할이 각각 실제 도구로 기여하고 고정 게이트가 승인한다 (AC-02)."""
    session = make_session(tmp_path)
    model = make_model(session, happy_path_script())
    result = orchestrate(session=session, model=model)

    assert result.status is RunStatus.VERIFIED_REPAIRED
    assert all(result.decision.checks.values())

    used = {run.envelope.agent_id: run for run in result.delegations}
    assert set(used) == set(DELEGATION_TARGETS), "네 역할이 모두 기여해야 한다"
    for run in used.values():
        assert run.tool_calls, f"{run.envelope.agent_id} 가 도구를 쓰지 않았다"
        assert run.result.summary, "고정 문구만 반환하면 기여로 볼 수 없다"


@requires_docker
def test_per_role_call_counts_match_ledger(tmp_path) -> None:
    """AC-16: 역할별 호출 수 합계가 gateway 원장과 일치한다."""
    session = make_session(tmp_path)
    model = make_model(session, happy_path_script())
    orchestrate(session=session, model=model)

    per_role = model.per_role_calls()
    usage = session.ledger.usage()
    assert sum(per_role.values()) == usage["model_calls"]
    assert per_role == {k: v for k, v in usage["per_role_calls"].items() if v}
    assert model.reconcile(per_role)["matches"]


@requires_docker
def test_auditor_never_receives_repair_rationale(tmp_path) -> None:
    """R-07 / AC-04: 감사 입력에 수리자의 설명이 유입되면 안 된다."""
    session = make_session(tmp_path)
    model = make_model(session, happy_path_script())
    orchestrate(session=session, model=model)

    audit = next(
        r for r in session.events.read(session.paths.run_dir)
        if r.type is EventType.DELEGATION_STARTED
        and r.data.get("agent_id") == "data_auditor"
    )
    visible = set(audit.data.get("evidence_ids") or [])
    private = {
        e.evidence_id
        for e in session.evidence.visible_to("repair_engineer")
        if str(e.visibility) == "repair_private"
    }
    assert private, "수리 근거가 저장돼 있어야 이 검사가 의미를 갖는다"
    assert not (visible & private), "감사자에게 수리 설명이 전달됐다"


@requires_docker
def test_auditor_objective_is_rewritten_by_runtime(tmp_path) -> None:
    """감사 objective 는 main 의 자유 텍스트가 아니라 고정 템플릿이다."""
    session = make_session(tmp_path)
    model = make_model(session, happy_path_script())
    orchestrate(session=session, model=model)

    started = next(
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.DELEGATION_STARTED
        and e.data.get("agent_id") == "data_auditor"
    )
    assert "런타임이 덮어쓴다" not in started.summary


@requires_docker
def test_finish_without_audit_is_returned_to_main(tmp_path) -> None:
    """AC-04: main 이 감사 없이 종료를 요청하면 게이트가 되돌려보낸다."""
    session = make_session(tmp_path)
    script = [
        j({"action": "delegate", "agent_id": "repair_engineer",
           "objective": "바로 고친다", "reason": "원인이 뻔하다"}),
        j({"tool": "submit_patch", "args": {"source": connector("healthy")}}),
        j({"outcome": "completed", "summary": "고쳤다."}),
        j({"action": "request_finish", "reason": "다 됐다"}),
        j({"action": "request_stop", "stop_reason": "verification_inconclusive",
           "reason": "감사를 붙일 예산이 없다"}),
    ]
    result = orchestrate(session=session, model=make_model(session, script))

    rejected = [
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.GATE_EVALUATED
        and e.data.get("rejection") == str(GateRejection.MISSING_AUDIT)
    ]
    assert rejected, "감사 없는 종료 요청은 MISSING_AUDIT 여야 한다"
    assert result.status is not RunStatus.VERIFIED_REPAIRED


@requires_docker
def test_repeated_delegation_without_new_evidence_is_rejected(tmp_path) -> None:
    """PRD §5.3.3: 같은 역할·질문·근거·후보의 재위임은 상한보다 먼저 거절된다."""
    session = make_session(tmp_path)
    same = {"action": "delegate", "agent_id": "spec_researcher",
            "objective": "응답 구조를 알려줘", "reason": "확인"}
    script = [
        j(same),
        j({"tool": "search_spec", "args": {"question": "구조"}}),
        j({"outcome": "completed", "summary": "확인함"}),
        j(same),
        j(same),
        j({"action": "request_stop", "stop_reason": "verification_inconclusive",
           "reason": "진전이 없다"}),
    ]
    orchestrate(session=session, model=make_model(session, script))

    rejections = [
        e for e in session.events.read(session.paths.run_dir)
        if e.type is EventType.DELEGATION_REJECTED
        and e.data.get("reason") == "NO_NEW_EVIDENCE"
    ]
    assert rejections, "중복 위임이 거절되지 않았다"
    assert session.ledger.role_delegations["spec_researcher"] == 1, \
        "거절된 위임은 예산을 쓰지 않아야 한다"


@requires_docker
def test_unresolved_audit_issue_blocks_success(tmp_path) -> None:
    """감사가 issue 를 남기면 main 동의로 덮지 못한다 (PRD §3.2 규칙 9)."""
    session = make_session(tmp_path)
    script = happy_path_script()
    script[12] = j({"outcome": "completed", "summary": "경계에서 손실이 보인다.",
        "findings": [
            {"risk_id": "pagination_boundary", "hypothesis": "마지막 구간 누락",
             "invariant": "partition_invariance", "conclusion": "issue",
             "probe_result_ids": ["pr_page_partition"]},
            {"risk_id": "mapping_preservation", "hypothesis": "중첩 경로",
             "invariant": "required_fields_present", "conclusion": "no_issue",
             "probe_result_ids": ["pr_field_presence"]}]})
    result = orchestrate(session=session, model=make_model(session, script))

    assert result.status is RunStatus.VERIFICATION_INCONCLUSIVE
    assert result.decision.rejection is GateRejection.UNRESOLVED_FINDING


def test_inventory_rejects_forbidden_tools() -> None:
    """AC-01: 프레임워크가 몰래 싣는 셸·파일 도구를 초기화 시점에 잡는다."""
    from api_doctor.tools.gateway import TOOL_ACL

    gateway = ToolGateway(ledger=BudgetLedger())
    for tool in Tool:
        gateway.register(tool, lambda context, **kw: None)
    assert_inventory(gateway)          # 정상

    original = TOOL_ACL["main"]
    try:
        TOOL_ACL["main"] = original | {"execute"}
        gateway.register("execute", lambda context, **kw: None)
        with pytest.raises(InventoryViolation, match="execute"):
            assert_inventory(gateway)
    finally:
        TOOL_ACL["main"] = original


def test_exactly_five_agents_and_four_targets() -> None:
    assert len(REGISTERED_AGENTS) == 5
    assert len(DELEGATION_TARGETS) == 4
    assert "general-purpose" not in REGISTERED_AGENTS


def test_lead_cannot_delegate_to_unregistered_agent() -> None:
    with pytest.raises(ProtocolError, match="등록되지 않은 위임 대상"):
        parse_lead_decision(
            '{"action":"delegate","agent_id":"general-purpose","objective":"x"}'
        )
