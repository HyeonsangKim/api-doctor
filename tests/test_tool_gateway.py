"""도구 게이트웨이와 증거 가시성 (AC-01, AC-04, AC-07)."""

from __future__ import annotations

import pytest

from api_doctor.runtime.budget import BudgetLedger
from api_doctor.runtime.evidence import (
    AUDIT, DIAG, MAIN, REPAIR, SPEC, EvidenceError, EvidenceKind, EvidenceStore,
    Visibility, can_read,
)
from pathlib import Path as _Path

from api_doctor.tools.gateway import (
    ACTION_ACL, TOOL_ACL, Action, Lease, Tool, ToolContext, ToolError, ToolGateway,
)

from _docker import requires_docker

_TESTS_DIR = _Path(__file__).resolve().parent

ROLES = (MAIN, SPEC, DIAG, REPAIR, AUDIT)


def _context(agent_id: str, candidate_hash: str = "sha256:v1") -> ToolContext:
    return ToolContext(
        run_id="run_x", task_id="task_x", agent_id=agent_id,
        candidate_hash=candidate_hash, contract_hash="sha256:c",
        snapshot_hash="sha256:s", lease=Lease(2, 1, 300.0),
    )


def test_product_team_is_exactly_main_plus_four() -> None:
    """AC-01: 제품 팀은 main + 4 뿐이다.

    비교 실험 B 의 단일 에이전트는 팀 밖의 대조군이므로 여기서 제외한다.
    그것이 위임 대상이 아니라는 것은 아래 테스트가 따로 확인한다.
    """
    from api_doctor.agents.inventory import CONTROL_AGENTS

    team = set(TOOL_ACL) - set(CONTROL_AGENTS)
    assert team == set(ROLES)
    assert len(team) == 5


def test_control_agent_is_not_part_of_the_team() -> None:
    """대조군은 위임 대상도 아니고 위임하지도 못한다 (PRD §7.3)."""
    from api_doctor.agents.inventory import (
        CONTROL_AGENTS, DELEGATION_TARGETS, REGISTERED_AGENTS,
    )

    for control in CONTROL_AGENTS:
        assert control not in REGISTERED_AGENTS
        assert control not in DELEGATION_TARGETS
        assert ACTION_ACL.get(control) == frozenset()
        with pytest.raises(ToolError):
            _context(control).authorize_action(Action.DELEGATE)


def test_control_agent_still_cannot_see_expectations() -> None:
    """같은 자료를 주되 정답은 주지 않는다."""
    from api_doctor.agents.inventory import CONTROL_AGENTS
    from api_doctor.runtime.evidence import Visibility, can_read

    for control in CONTROL_AGENTS:
        assert not can_read(control, Visibility.VERIFIER_ONLY)


def test_only_main_can_delegate() -> None:
    """AC-01: 하위 재위임·동적 생성은 구조적으로 불가능하다."""
    assert ACTION_ACL[MAIN] == {Action.DELEGATE, Action.REVISE_PLAN}
    for role in (SPEC, DIAG, REPAIR, AUDIT):
        assert ACTION_ACL[role] == frozenset()
        with pytest.raises(ToolError, match="재위임"):
            _context(role).authorize_action(Action.DELEGATE)


def test_only_repair_can_patch() -> None:
    _context(REPAIR).authorize(Tool.SUBMIT_PATCH)
    for role in (MAIN, SPEC, DIAG, AUDIT):
        with pytest.raises(ToolError) as exc:
            _context(role).authorize(Tool.SUBMIT_PATCH)
        assert exc.value.code == "FORBIDDEN"


def test_only_main_can_request_finish_or_stop() -> None:
    """main 도 성공을 확정할 수 없고, 다른 역할은 요청조차 못 한다."""
    for tool in (Tool.REQUEST_FINISH, Tool.REQUEST_STOP):
        _context(MAIN).authorize(tool)
        for role in (SPEC, DIAG, REPAIR, AUDIT):
            with pytest.raises(ToolError):
                _context(role).authorize(tool)


def test_spec_researcher_cannot_touch_sandbox_or_code() -> None:
    for tool in (Tool.RUN_PROBE, Tool.SUBMIT_PATCH, Tool.INSPECT_CODE):
        with pytest.raises(ToolError):
            _context(SPEC).authorize(tool)


def test_forged_agent_id_in_arguments_is_ignored() -> None:
    """AC-07: 모델이 role 값을 위조해도 실행 컨텍스트 권한으로 판정한다."""
    ledger = BudgetLedger()
    gateway = ToolGateway(ledger=ledger)
    seen: list[str] = []

    def impl(context: ToolContext, **kwargs) -> str:
        seen.append(context.agent_id)
        assert "agent_id" not in kwargs, "예약 인자가 구현까지 새면 안 된다"
        return "ok"

    gateway.register(Tool.RUN_PROBE, impl)
    bound = gateway.bind(_context(AUDIT))
    assert bound["run_probe"](agent_id="repair_engineer", probe_id="p") == "ok"
    assert seen == [AUDIT], "인자로 넘긴 역할이 아니라 컨텍스트의 역할이 쓰여야 한다"


def test_bind_only_exposes_permitted_tools() -> None:
    """전문가는 자기 것이 아닌 도구의 이름조차 받지 않는다."""
    gateway = ToolGateway(ledger=BudgetLedger())
    for tool in Tool:
        gateway.register(tool, lambda context, **kw: None)
    assert "submit_patch" in gateway.bind(_context(REPAIR))
    assert "submit_patch" not in gateway.bind(_context(AUDIT))
    assert "delegate" not in gateway.bind(_context(AUDIT))


def test_denied_tool_calls_are_logged() -> None:
    gateway = ToolGateway(ledger=BudgetLedger())
    gateway.register(Tool.SUBMIT_PATCH, lambda context, **kw: "patched")
    bound = gateway.bind(_context(REPAIR))
    bound["submit_patch"](diff="x")
    entry = gateway.call_log[-1]
    assert entry["allowed"] and entry["agent_id"] == REPAIR


def test_auditor_cannot_see_repair_explanation(tmp_path) -> None:
    """AC-04 / R-07: 감사 입력에 수리자의 설명이 유입되면 안 된다."""
    store = EvidenceStore(tmp_path)
    private = store.add(
        kind=EvidenceKind.ASSERTION, visibility=Visibility.REPAIR_PRIVATE,
        source="repair_engineer", created_by=REPAIR,
        summary="왜 이렇게 고쳤는지", body={"reason": "중첩 경로"},
    )
    with pytest.raises(EvidenceError) as exc:
        store.read(private.evidence_id, agent_id=AUDIT)
    assert exc.value.code == "FORBIDDEN"
    assert private.evidence_id not in {e.evidence_id for e in store.visible_to(AUDIT)}


def test_nobody_can_read_verifier_expectations() -> None:
    """기대값은 어떤 에이전트도 볼 수 없다."""
    for role in ROLES:
        assert not can_read(role, Visibility.VERIFIER_ONLY)


def test_stale_evidence_is_refused_for_new_candidate(tmp_path) -> None:
    """AC-13: 이전 hash 의 결과를 최신 검증으로 재사용하지 않는다."""
    store = EvidenceStore(tmp_path)
    observation = store.add(
        kind=EvidenceKind.OBSERVATION, visibility=Visibility.RUNTIME_ONLY,
        source="run_probe", created_by=DIAG, summary="v1 관측",
        body={"rows": 10}, candidate_hash="sha256:v1",
    )
    store.read(observation.evidence_id, agent_id=AUDIT, candidate_hash="sha256:v1")
    with pytest.raises(EvidenceError) as exc:
        store.read(observation.evidence_id, agent_id=AUDIT, candidate_hash="sha256:v2")
    assert exc.value.code == "STALE"


def test_assertions_are_distinguished_from_observations(tmp_path) -> None:
    """모델의 주장은 관측으로 승격되지 않는다 (PRD §5.2)."""
    store = EvidenceStore(tmp_path)
    store.add(kind=EvidenceKind.ASSERTION, visibility=Visibility.PUBLIC,
              source="model", created_by=AUDIT, summary="괜찮아 보입니다",
              body={"claim": "no issue"}, candidate_hash="sha256:v1")
    assert store.observations_for("sha256:v1") == []


@requires_docker
def test_probe_result_tells_auditor_what_remains(tmp_path) -> None:
    """감사자에게 남은 위험 영역을 도구 결과로 알려준다.

    실측에서 가장 잦은 실패가 "고정 검증은 통과했는데 감사가 한쪽 영역만
    덮어 MISSING_AUDIT" 였다. 판정은 여전히 종료 게이트가 한다.
    """
    import sys

    sys.path.insert(0, str(_TESTS_DIR))
    from _harness import make_session

    from api_doctor.runtime.envelope import _AUDIT_PROBES
    from api_doctor.tools.gateway import make_lease
    from api_doctor.tools.impl import register_all

    session = make_session(tmp_path, start="healthy")
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    context = ToolContext(
        run_id=session.run_id, task_id="t", agent_id=AUDIT,
        candidate_hash=session.current_hash,
        contract_hash=session.dataset.contract.contract_hash,
        snapshot_hash=session.snapshot.snapshot_hash,
        lease=make_lease(session.ledger, AUDIT),
        allowed_probe_ids=_AUDIT_PROBES,
    )
    bound = gateway.bind(context)

    first = bound["run_probe"](probe_id="page_partition")["audit_coverage"]
    assert first["remaining_risks"] == ["mapping_preservation"]
    assert first["probes_for_remaining"]["mapping_preservation"] == ["field_presence"]

    second = bound["run_probe"](probe_id="field_presence")["audit_coverage"]
    assert second["remaining_risks"] == []


@requires_docker
def test_coverage_hint_is_not_given_to_other_roles(tmp_path) -> None:
    """감사 안내는 감사자에게만 간다."""
    import sys

    sys.path.insert(0, str(_TESTS_DIR))
    from _harness import make_session

    from api_doctor.tools.gateway import make_lease
    from api_doctor.tools.impl import register_all

    session = make_session(tmp_path, start="healthy")
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    context = ToolContext(
        run_id=session.run_id, task_id="t", agent_id=DIAG,
        candidate_hash=session.current_hash,
        contract_hash=session.dataset.contract.contract_hash,
        snapshot_hash=session.snapshot.snapshot_hash,
        lease=make_lease(session.ledger, DIAG),
    )
    result = gateway.bind(context)["run_probe"](probe_id="page_partition")
    assert "audit_coverage" not in result


# ------------------------------------------------------- 파싱되지 않는 패치


def _repair_gateway(tmp_path):
    """수리자 컨텍스트에 바인딩된 도구."""
    import sys

    sys.path.insert(0, str(_TESTS_DIR))
    from _harness import make_session

    from api_doctor.tools.gateway import make_lease
    from api_doctor.tools.impl import register_all

    session = make_session(tmp_path)
    gateway = ToolGateway(ledger=session.ledger)
    register_all(gateway, session)
    context = ToolContext(
        run_id=session.run_id, task_id="t", agent_id=REPAIR,
        candidate_hash=session.current_hash,
        contract_hash=session.dataset.contract.contract_hash,
        snapshot_hash=session.snapshot.snapshot_hash,
        lease=make_lease(session.ledger, REPAIR),
    )
    return session, gateway.bind(context)


@requires_docker
def test_unparsable_patch_is_rejected_before_spending_the_limit(tmp_path) -> None:
    """파싱되지 않는 패치는 패치 한도를 쓰지 않고 되돌려보낸다.

    실측(2026-09-28): 수리자가 코드에 가운뎃점(·)을 넣어 SyntaxError 로
    실행이 죽었고 고정 검증 4종이 전부 실패했다. 한도를 먼저 쓰면
    수리자에게 다시 낼 기회가 없다.
    """
    session, bound = _repair_gateway(tmp_path)
    before = session.ledger.patches

    with pytest.raises(ToolError) as caught:
        bound["submit_patch"](
            source="def fetch_records(http, query):\n    a = 1 \u00b7 2\n    return []\n"
        )

    assert caught.value.code == "INVALID_PATCH"
    assert "가운뎃점" in str(caught.value), "무엇이 틀렸는지 알려줘야 다시 낸다"
    assert session.ledger.patches == before, "거절된 패치가 한도를 먹었다"


@requires_docker
def test_valid_patch_still_goes_through(tmp_path) -> None:
    """구문 검사가 정상 패치를 막으면 안 된다."""
    session, bound = _repair_gateway(tmp_path)
    result = bound["submit_patch"](
        source="def fetch_records(http, query):\n    return [{'LBRRY_SEQ_NO': '1'}]\n"
    )
    assert result["candidate_hash"]
    assert session.ledger.patches == 1


@requires_docker
def test_plain_syntax_error_is_reported_without_a_false_hint(tmp_path) -> None:
    """서술용 문자가 없는데 있다고 하면 수리자를 엉뚱한 데로 보낸다."""
    _, bound = _repair_gateway(tmp_path)
    with pytest.raises(ToolError) as caught:
        bound["submit_patch"](
            source="def fetch_records(http, query)\n    return []\n"
        )
    assert "서술용 문자" not in str(caught.value)
