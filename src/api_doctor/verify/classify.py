"""공급자 응답 분류기 (FR-006, FR-013, AC-06).

인증 만료·한도 소진·제공기관 장애를 **모델 판단보다 먼저** 확정한다.
PRD §3.3: "신뢰된 시스템은 인증·정책·예산 오류를 모델 판단보다 먼저 중단한다."

이 모듈은 `agents` · `tools` · `model` 을 import 하지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# `/json/` 으로 요청해도 오류는 XML 로 오는 것이 실측으로 확인됐다.
_XML_CODE = re.compile(r"<CODE>\s*([A-Z]+-\d+)\s*</CODE>", re.I)
_XML_MESSAGE = re.compile(
    r"<MESSAGE>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</MESSAGE>", re.I | re.S
)


class Classification(StrEnum):
    OK = "ok"
    AUTH = "auth"
    QUOTA = "quota"
    EMPTY = "empty"
    BAD_REQUEST = "bad_request"
    PROVIDER_FAULT = "provider_fault"
    UNKNOWN = "unknown"

    @property
    def is_blocking(self) -> bool:
        """복구를 시도하면 안 되는 분류인가.

        코드를 고쳐서 해결될 문제가 아니므로 모델을 부르지 않는다 (AC-06).
        """
        return self in (
            Classification.AUTH, Classification.QUOTA,
            Classification.PROVIDER_FAULT,
        )


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    classification: Classification
    code: str | None
    message: str
    body_kind: str   # json | xml | unknown

    def to_json(self) -> dict[str, Any]:
        return {
            "classification": str(self.classification),
            "code": self.code,
            "message": self.message[:300],
            "body_kind": self.body_kind,
        }


def classify_body(body: str, codes: dict[str, str]) -> ProviderResponse:
    """응답 본문 1건을 분류한다.

    등록된 코드표만 쓴다. 모델이 이 표를 만들거나 바꾸지 않는다.
    """
    text = (body or "").strip()
    if not text:
        return ProviderResponse(Classification.UNKNOWN, None, "빈 응답", "unknown")

    code: str | None = None
    message = ""
    kind = "unknown"

    if text.startswith("<"):
        kind = "xml"
        found = _XML_CODE.search(text)
        code = found.group(1).upper() if found else None
        said = _XML_MESSAGE.search(text)
        message = (said.group(1) if said else "").strip()
    else:
        kind = "json"
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return ProviderResponse(
                Classification.UNKNOWN, None, "본문을 해석할 수 없습니다.", kind
            )
        result = _find_result(data)
        if result:
            code = str(result.get("CODE", "")).upper() or None
            message = str(result.get("MESSAGE", ""))

    if code is None:
        return ProviderResponse(Classification.UNKNOWN, None, message, kind)

    mapped = codes.get(code)
    if mapped is None:
        return ProviderResponse(Classification.UNKNOWN, code, message, kind)
    try:
        return ProviderResponse(Classification(mapped), code, message, kind)
    except ValueError:
        return ProviderResponse(Classification.UNKNOWN, code, message, kind)


def _find_result(data: Any, depth: int = 0) -> dict[str, Any] | None:
    """`RESULT` 를 최상위 또는 서비스명 한 겹 아래에서 찾는다."""
    if depth > 2 or not isinstance(data, dict):
        return None
    if isinstance(data.get("RESULT"), dict):
        return data["RESULT"]
    for value in data.values():
        found = _find_result(value, depth + 1)
        if found is not None:
            return found
    return None


def classify_run(bodies: list[str], codes: dict[str, str]) -> ProviderResponse:
    """실행 중 받은 응답들에서 **가장 먼저 막는 분류**를 고른다.

    한 건이라도 인증·한도·장애면 그 run 은 복구 대상이 아니다.
    """
    seen: list[ProviderResponse] = [classify_body(b, codes) for b in bodies]
    for response in seen:
        if response.classification.is_blocking:
            return response
    for response in seen:
        if response.classification is not Classification.OK:
            return response
    return seen[0] if seen else ProviderResponse(
        Classification.UNKNOWN, None, "응답이 없습니다.", "unknown"
    )
