"""모델 출력에서 JSON 을 꺼내는 저수준 헬퍼.

`model` 과 `agents` 가 모두 쓰므로 어느 쪽에도 속하지 않는다.
의존 방향(아키텍처 §3.1)을 지키기 위한 분리다.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class ProtocolError(RuntimeError):
    """구조화 반환이 계약을 만족하지 않습니다."""


def parse_json_object(text: str) -> dict[str, Any]:
    """모델 출력에서 JSON 객체 하나를 꺼낸다.

    코드펜스·앞뒤 설명을 관대하게 다루되, **객체가 아니면 거절**한다.
    관대함은 형식에만 적용하고 내용에는 적용하지 않는다.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ProtocolError("모델 출력에서 JSON 객체를 찾지 못했습니다.")
