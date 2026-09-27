"""공급자 응답 분류기 (FR-006, FR-013, AC-06).

본문은 2026-09-27 서울 열린데이터광장에 실제로 호출해 받은 것이다.
"""

from __future__ import annotations

import json

import pytest

from api_doctor.registry.loader import load_registry
from api_doctor.verify.classify import (
    Classification, classify_body, classify_run,
)

# 유효하지 않은 키로 실호출해 받은 응답. /json/ 요청인데 XML 로 온다.
AUTH_XML = (
    "<RESULT><CODE>INFO-100</CODE><MESSAGE><![CDATA[인증키가 유효하지 않습니다.\n"
    "인증키가 없는 경우, 열린 데이터 광장 홈페이지에서 인증키를 신청하십시오.]]>"
    "</MESSAGE></RESULT>"
)
SERVER_JSON = json.dumps(
    {"RESULT": {"CODE": "ERROR-500", "MESSAGE": "서버 오류입니다."}}, ensure_ascii=False
)
SAMPLE_LIMIT_XML = (
    "<RESULT><CODE>ERROR-335</CODE><MESSAGE><![CDATA[샘플데이터는 최대 5건]]>"
    "</MESSAGE></RESULT>"
)
OK_JSON = json.dumps(
    {"SeoulPublicLibraryInfo": {
        "list_total_count": 216,
        "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
        "row": []}},
    ensure_ascii=False,
)


@pytest.fixture
def codes():
    return load_registry()["seoul_library"].provider_errors


def test_auth_error_is_blocking(codes) -> None:
    result = classify_body(AUTH_XML, codes)
    assert result.classification is Classification.AUTH
    assert result.classification.is_blocking
    assert result.code == "INFO-100"
    assert result.body_kind == "xml"


def test_xml_error_on_json_endpoint_is_parsed(codes) -> None:
    """`/json/` 요청에도 오류가 XML 로 온다 — 실측 확인된 동작."""
    assert classify_body(AUTH_XML, codes).body_kind == "xml"


def test_provider_fault_is_blocking(codes) -> None:
    result = classify_body(SERVER_JSON, codes)
    assert result.classification is Classification.PROVIDER_FAULT
    assert result.classification.is_blocking


def test_bad_request_is_not_blocking(codes) -> None:
    """범위 오류는 코드를 고쳐서 해결할 수 있으므로 복구 대상이다."""
    result = classify_body(SAMPLE_LIMIT_XML, codes)
    assert result.classification is Classification.BAD_REQUEST
    assert not result.classification.is_blocking


def test_nested_ok_result_is_found(codes) -> None:
    """RESULT 가 서비스명 한 겹 아래에 있어도 찾는다."""
    result = classify_body(OK_JSON, codes)
    assert result.classification is Classification.OK
    assert result.code == "INFO-000"


def test_unregistered_code_is_unknown(codes) -> None:
    """등록 표에 없는 코드를 임의로 해석하지 않는다."""
    body = "<RESULT><CODE>ERROR-999</CODE><MESSAGE>모르는 코드</MESSAGE></RESULT>"
    result = classify_body(body, codes)
    assert result.classification is Classification.UNKNOWN
    assert result.code == "ERROR-999"


def test_blocking_wins_over_ok_in_a_run(codes) -> None:
    """한 건이라도 인증이 막혔으면 그 run 은 복구 대상이 아니다."""
    result = classify_run([OK_JSON, AUTH_XML, OK_JSON], codes)
    assert result.classification is Classification.AUTH


def test_all_ok_run_is_ok(codes) -> None:
    assert classify_run([OK_JSON, OK_JSON], codes).classification is Classification.OK


def test_empty_body_is_unknown_not_ok(codes) -> None:
    assert classify_body("", codes).classification is Classification.UNKNOWN
