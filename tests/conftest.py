"""테스트 스위트를 밀폐한다.

어떤 테스트도 실제 모델 API 를 호출해서는 안 된다. 개발자 환경에
`NVIDIA_API_KEY` 가 설정돼 있으면 CLI 경로가 실제 호출을 시도하게 되므로
모든 테스트에서 지운다. 실모델 trace 가 필요한 검증은 별도 수동 절차다.
"""

from __future__ import annotations

import pytest

_NETWORK_ENV = ("NVIDIA_API_KEY",)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for name in _NETWORK_ENV:
        monkeypatch.delenv(name, raising=False)
