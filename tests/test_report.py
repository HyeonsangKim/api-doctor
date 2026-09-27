"""보고서 (FR-015, FR-017, AC-15)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_doctor.model.gateway import ScriptedBackend
from api_doctor.runtime.events import RunStatus
from api_doctor.runtime.recover import recover
from api_doctor.runtime.store import RunStore
from api_doctor.report.sanitize import sanitize

from _docker import requires_docker
from _harness import happy_path_script

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _recover(tmp_path, name: str, script: list[str] | None = None):
    return recover(
        dataset_id="seoul_library",
        code_path=EXAMPLES / f"connector_{name}.py",
        store=RunStore(tmp_path / "runs"),
        model_backend=(
            ScriptedBackend(
                responses=script,
                usage_per_call={"input_tokens": 900, "output_tokens": 180,
                                "total_tokens": 1080},
            ) if script else None
        ),
        scripted=bool(script),
    )


@requires_docker
def test_full_pipeline_produces_report(tmp_path) -> None:
    result = _recover(tmp_path, "broken", happy_path_script())
    assert result.status is RunStatus.VERIFIED_REPAIRED

    markdown = result.report_paths[0].read_text(encoding="utf-8")
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))

    assert "scripted" in markdown, "scripted 실행은 실제 모델 trace 와 구분해야 한다"
    assert payload["model_trace_kind"] == "scripted"
    assert payload["source"]["kind"] == "live_capture"
    assert payload["hashes"]["final_candidate"] != payload["hashes"]["original"]


@requires_docker
def test_report_shows_every_role_contribution(tmp_path) -> None:
    """FR-017: 이름만 등장하는 역할을 구분해 드러낸다."""
    result = _recover(tmp_path, "broken", happy_path_script())
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))

    contributions = {c["agent_id"]: c for c in payload["contributions"]}
    assert len(contributions) == 4
    for agent_id, row in contributions.items():
        assert row["produced_observation"], f"{agent_id} 가 관측을 남기지 않았다"
        assert row["tools_used"], f"{agent_id} 가 도구를 쓰지 않았다"


@requires_docker
def test_per_role_usage_reconciles_in_report(tmp_path) -> None:
    """AC-16: 역할별 합계와 원장이 일치함을 보고서가 밝힌다."""
    result = _recover(tmp_path, "broken", happy_path_script())
    markdown = result.report_paths[0].read_text(encoding="utf-8")
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))

    usage = payload["usage"]
    assert sum(usage["per_role_calls"].values()) == usage["model_calls"]
    assert "일치합니다" in markdown


@requires_docker
def test_container_denials_are_never_labeled_openshell(tmp_path) -> None:
    """AC-15: 컨테이너 결과를 OpenShell 증거로 표시하지 않는다."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text(
        "import urllib.request\n\n"
        "def fetch_records(http, query):\n"
        "    urllib.request.urlopen('http://example.com', timeout=3)\n"
        "    return []\n",
        encoding="utf-8",
    )
    result = recover(
        dataset_id="seoul_library", code_path=leaky,
        store=RunStore(tmp_path / "runs"),
    )
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))
    markdown = result.report_paths[0].read_text(encoding="utf-8")

    assert payload["policy_denials"], "차단 기록이 수집돼야 한다"
    for denial in payload["policy_denials"]:
        assert denial["backend_id"] == "container"
    assert "OpenShell 정책 차단" not in markdown
    assert "OpenShell 차단 증거로 사용하지 않습니다" in markdown


@requires_docker
def test_no_denials_does_not_claim_success(tmp_path) -> None:
    result = _recover(tmp_path, "healthy")
    markdown = result.report_paths[0].read_text(encoding="utf-8")
    assert "차단 성공을 주장하지 않습니다" in markdown


@requires_docker
def test_missing_model_key_is_reported_honestly(tmp_path, monkeypatch) -> None:
    """복구를 시도하지 않았으면 그렇게 적는다."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    result = _recover(tmp_path, "broken")
    assert result.status is RunStatus.NEEDS_USER_ACTION
    assert not result.model_available
    assert "NVIDIA_API_KEY" in (result.model_error or "")

    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))
    assert payload["usage"]["model_calls"] == 0
    assert payload["contributions"] == []
    # baseline 판정은 남아야 한다
    assert payload["verification"] is not None


@requires_docker
def test_healthy_code_reports_no_delegation(tmp_path) -> None:
    """AC-05: 정상 코드는 모델도 전문 역할도 부르지 않는다."""
    result = _recover(tmp_path, "healthy")
    assert result.status is RunStatus.VERIFIED_UNCHANGED
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))
    assert payload["usage"]["model_calls"] == 0
    assert payload["contributions"] == []
    assert "v1.py" not in " ".join(result.artifacts)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x1b[31mRED\x1b[0mb", "aREDb"),
        ("cell|breaks", "cell\\|breaks"),
        ("<script>x</script>", "&lt;script>x&lt;/script>"),
        ("line1\nline2", "line1 line2"),
    ],
)
def test_sanitize_neutralizes_hostile_strings(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


@requires_docker
def test_single_ledger_across_baseline_and_recovery(tmp_path) -> None:
    """FR-003: 원장이 하나여야 모든 호출이 같은 gateway 를 통과한다."""
    result = _recover(tmp_path, "broken", happy_path_script())
    payload = json.loads(result.report_paths[1].read_text(encoding="utf-8"))
    usage = payload["usage"]
    # baseline 1회 + probe·종료검증이 같은 원장에 누적됐어야 한다
    assert usage["sandbox_runs"] > 1
    assert usage["broker_calls"] > 0
