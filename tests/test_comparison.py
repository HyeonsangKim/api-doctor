"""비교 실험 B vs C (PRD §7.3).

**대조군이 제품 구조가 아님**을 지키는 것이 요점이다.
같은 모델·자료·도구·검증·예산을 쓰되 역할만 나누지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.agents.inventory import CONTROL_AGENTS, DELEGATION_TARGETS
from api_doctor.model.gateway import ScriptedBackend
from api_doctor.runtime.budget import BudgetLedger
from api_doctor.runtime.events import RunStatus
from api_doctor.runtime.recover import recover
from api_doctor.runtime.store import RunStore
from api_doctor.tools.gateway import TOOL_ACL

from _docker import requires_docker
from _harness import connector, deep_happy_script, j

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
USAGE = {"input_tokens": 900, "output_tokens": 180, "total_tokens": 1080}

SINGLE_SCRIPT = [
    j({"tool": "search_spec", "args": {"question": "중첩 경로와 범위 종료"}}),
    j({"tool": "run_probe", "args": {"probe_id": "range_coverage"}}),
    j({"tool": "submit_patch", "args": {"source": connector("healthy"),
                                        "rationale": "두 결함"}}),
    j({"tool": "run_probe", "args": {"probe_id": "page_partition"}}),
    j({"tool": "run_probe", "args": {"probe_id": "field_presence"}}),
    j({"outcome": "completed", "summary": "고치고 확인했다.", "findings": [
        {"risk_id": "pagination_boundary", "hypothesis": "마지막 구간",
         "invariant": "partition_invariance", "conclusion": "no_issue",
         "probe_result_ids": ["pr1"]},
        {"risk_id": "mapping_preservation", "hypothesis": "중첩 경로",
         "invariant": "required_fields_present", "conclusion": "no_issue",
         "probe_result_ids": ["pr2"]}]}),
]


def _run(tmp_path, harness, script):
    return recover(
        dataset_id="seoul_library",
        code_path=EXAMPLES / "connector_broken.py",
        store=RunStore(tmp_path / "runs"),
        model_backend=ScriptedBackend(responses=script, usage_per_call=USAGE),
        scripted=True, harness=harness,
    )


def test_single_agent_gets_the_same_tool_power() -> None:
    """정보 차이로 팀을 유리하게 만들지 않는다 (PRD §7.3)."""
    team_tools = set().union(*(TOOL_ACL[a] for a in DELEGATION_TARGETS))
    single_tools = TOOL_ACL["single_agent"]
    assert team_tools <= single_tools, "대조군이 팀보다 적은 도구를 갖는다"


def test_single_agent_gets_the_same_total_budget() -> None:
    """PRD §7.3: 같은 **총예산**을 준다.

    역할별 상한의 합은 전체 상한보다 크다. 실측 후 역할별 여유를 늘렸기
    때문이며, 실질 제약은 전체 호출 수다. 대조군에게는 그 전체 상한을
    그대로 준다 — 역할이 하나뿐이니 나눌 것이 없다.
    """
    limits = BudgetLedger().limits
    assert limits.role_model_calls["single_agent"] == limits.model_calls

    team_ceiling = sum(
        v for k, v in limits.role_model_calls.items() if k not in CONTROL_AGENTS
    )
    assert team_ceiling >= limits.model_calls, (
        "역할별 상한의 합이 전체보다 작으면 전체 상한이 도달 불가능해진다"
    )


@requires_docker
def test_both_arms_pass_the_same_fixed_gate(tmp_path) -> None:
    """두 조건 모두 같은 고정 검증기를 통과해야 성공이다."""
    team = _run(tmp_path / "c", "deepagents", deep_happy_script())
    single = _run(tmp_path / "b", "single", SINGLE_SCRIPT)

    assert team.status is RunStatus.VERIFIED_REPAIRED
    assert single.status is RunStatus.VERIFIED_REPAIRED
    for result in (team, single):
        assert all(result.orchestration.decision.checks.values())


@requires_docker
def test_comparison_reports_call_counts_side_by_side(tmp_path) -> None:
    """성공률만이 아니라 실제 호출·토큰을 함께 보고한다.

    PRD §7.3: 단발의 적은 호출량을 숨기지 않는다.
    """
    team = _run(tmp_path / "c", "deepagents", deep_happy_script())
    single = _run(tmp_path / "b", "single", SINGLE_SCRIPT)

    team_usage = json.loads(team.report_paths[1].read_text())["usage"]
    single_usage = json.loads(single.report_paths[1].read_text())["usage"]
    assert team_usage["model_calls"] > 0 and single_usage["model_calls"] > 0
    # 위임이 없으니 대조군의 호출이 더 적은 것이 정상이다.
    assert single_usage["model_calls"] < team_usage["model_calls"]


@requires_docker
def test_single_arm_is_reported_as_one_contributor(tmp_path) -> None:
    single = _run(tmp_path, "single", SINGLE_SCRIPT)
    payload = json.loads(single.report_paths[1].read_text())
    assert len(payload["contributions"]) == 1
    assert payload["contributions"][0]["agent_id"] == "single_agent"


@requires_docker
def test_single_arm_cannot_skip_the_audit_requirement(tmp_path) -> None:
    """혼자 한다고 감사 조건이 면제되지 않는다."""
    script = [
        j({"tool": "submit_patch", "args": {"source": connector("healthy")}}),
        j({"outcome": "completed", "summary": "고쳤으니 됐다."}),
    ]
    result = _run(tmp_path, "single", script)
    assert result.status is not RunStatus.VERIFIED_REPAIRED
    assert result.orchestration.decision.rejection is not None
