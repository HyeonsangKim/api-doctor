"""보고서 문자열 정리 (PRD §4.5).

외부 자료·모델 출력이 터미널이나 Markdown 뷰어를 조작하지 못하게 한다.
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HTML = re.compile(r"<(/?)(script|iframe|object|embed|style)\b", re.I)


def sanitize(text: str) -> str:
    """제어문자·escape sequence 를 지우고 표 구조를 깨는 문자를 막는다."""
    cleaned = _ANSI.sub("", str(text))
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = _HTML.sub(r"&lt;\1\2", cleaned)
    # Markdown 표 안에서 셀이 쪼개지지 않게 한다.
    return cleaned.replace("|", "\\|").replace("\n", " ").strip()
