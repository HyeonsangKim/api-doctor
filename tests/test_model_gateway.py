

"""모델 게이트웨이의 레이트리밋.

실측 복구 평가에서 시도의 73% 가 429 였다. 공급자 장애가 아니라 우리가
무료 등급의 요청 한도를 넘긴 것이고, 그 대가로 전역 실패 허용량이 조사
단계에서 소진되어 수리자가 한 번도 제대로 못 돌았다.
"""

from __future__ import annotations

import pytest

from api_doctor.model.gateway import (
    ModelConfig, ModelGateway, RateLimiter, ScriptedBackend,
)
from api_doctor.runtime.budget import BudgetLedger, Limits


def make_ledger() -> BudgetLedger:
    return BudgetLedger(limits=Limits())


def test_limiter_spaces_calls_apart() -> None:
    """호출 사이에 최소 간격을 둔다."""
    from api_doctor.model.gateway import MIN_CALL_INTERVAL

    clock = [0.0]
    r = RateLimiter(now=lambda: clock[0])
    assert r.due() == 0.0, "첫 호출은 기다리지 않는다"
    r.mark()
    assert r.due() == pytest.approx(MIN_CALL_INTERVAL)
    clock[0] += MIN_CALL_INTERVAL
    assert r.due() == 0.0


def test_limiter_widens_on_rate_limit_and_narrows_on_success() -> None:
    """429 를 맞으면 간격을 늘리고, 연속 성공하면 되돌린다."""
    from api_doctor.model.gateway import (
        MAX_CALL_INTERVAL, MIN_CALL_INTERVAL, REWARD_STREAK,
    )

    r = RateLimiter()
    before = r.interval
    r.penalize()
    assert r.interval > before, "429 뒤에도 같은 속도로 부르면 또 맞는다"

    for _ in range(40):
        r.penalize()
    assert r.interval <= MAX_CALL_INTERVAL, "무한히 느려지면 시간 예산을 태운다"

    for _ in range(REWARD_STREAK * 40):
        r.reward()
    assert r.interval == pytest.approx(MIN_CALL_INTERVAL), "안정되면 되돌아와야 한다"


def test_limiter_honors_retry_after_over_its_own_guess() -> None:
    """공급자가 말한 대기 시간이 우리 추정보다 우선한다."""
    assert RateLimiter().penalize(retry_after=12.0) == 12.0


def test_rate_limit_is_distinguished_from_provider_outage() -> None:
    """429 는 공급자 장애가 아니라 우리 잘못이다 — 대응이 달라야 한다."""
    from api_doctor.model.gateway import is_rate_limited, parse_retry_after

    assert is_rate_limited("HTTPStatusError: Client error '429 Too Many Requests'")
    assert is_rate_limited("too many requests")
    assert not is_rate_limited("HTTPStatusError: Server error '503'")
    assert parse_retry_after("429 ... 'retry-after': '7'") == 7.0
    assert parse_retry_after("503 Service Unavailable") is None


def test_gateway_paces_its_calls(monkeypatch) -> None:
    """게이트웨이가 실제로 간격을 두고 부른다 — 취소 토큰으로 기다린다."""
    ledger = make_ledger()
    waited: list[float] = []
    monkeypatch.setattr(
        type(ledger.cancel), "wait", lambda self, d: waited.append(d)
    )
    gw = ModelGateway(
        backend=ScriptedBackend(["첫 응답", "둘째 응답"]),
        ledger=ledger,
        limiter=RateLimiter(),   # 원격인 척 강제로 페이싱을 건다
    )
    gw.complete(agent_id="main", messages=[{"role": "user", "content": "안녕"}])
    gw.complete(agent_id="main", messages=[{"role": "user", "content": "안녕"}])
    assert waited and waited[0] > 0, "두 번째 호출이 곧바로 나갔다"


def test_only_remote_backends_are_paced(monkeypatch) -> None:
    """scripted backend 는 공급자에게 나가지 않으므로 재우지 않는다.

    이걸 구분하지 않으면 테스트만 느려지고 지켜지는 것은 없다.
    """
    ledger = make_ledger()
    waited: list[float] = []
    monkeypatch.setattr(type(ledger.cancel), "wait", lambda self, d: waited.append(d))
    gw = ModelGateway(backend=ScriptedBackend(["하나", "둘"]), ledger=ledger)
    assert gw.limiter.interval == 0.0
    gw.complete(agent_id="main", messages=[{"role": "user", "content": "안녕"}])
    gw.complete(agent_id="main", messages=[{"role": "user", "content": "안녕"}])
    assert not waited, "scripted backend 에 레이트리밋이 걸렸다"


def test_remote_backend_declares_itself_paced() -> None:
    """실제 공급자로 나가는 backend 는 반드시 페이싱 대상이어야 한다."""
    from api_doctor.model.gateway import NvidiaChatBackend

    assert NvidiaChatBackend.is_remote is True
    assert ScriptedBackend.is_remote is False


# ----------------------------------------------------------------- 폴백 모델


class FlakyBackend:
    """지정한 모델에 대해 정해진 오류를 내는 backend."""

    is_remote = True

    def __init__(self, failing: dict[str, str]) -> None:
        self.failing = failing
        self.seen: list[str] = []

    def complete(self, messages, *, max_tokens, timeout, model=None):
        self.seen.append(model or "")
        err = self.failing.get(model or "")
        if err:
            raise RuntimeError(err)
        return "고쳤습니다", {"input_tokens": 10, "output_tokens": 5}


def _no_wait(monkeypatch, ledger) -> None:
    monkeypatch.setattr(type(ledger.cancel), "wait", lambda self, d: None)


def test_overload_switches_to_the_fallback_model(monkeypatch) -> None:
    """503 은 우리가 만든 부하가 아니다 — 기다리지 말고 다른 모델로 간다.

    실측(2026-09-28): 주 모델 성공률 1/8, 폴백 4/8. 503 은 0.1초 만에
    즉시 거절되므로 같은 모델을 다시 불러도 결과가 같다.
    """
    from api_doctor.model.gateway import DEFAULT_FALLBACK_MODELS, ModelConfig

    ledger = make_ledger()
    _no_wait(monkeypatch, ledger)
    primary = ModelConfig().model
    backend = FlakyBackend({primary: "Server error '503' Service temporarily overloaded"})
    gw = ModelGateway(backend=backend, ledger=ledger, limiter=RateLimiter.unpaced())

    assert gw.complete(agent_id="main", messages=[{"role": "user", "content": "x"}])
    assert backend.seen[0] == primary
    assert backend.seen[1] == DEFAULT_FALLBACK_MODELS[0], "폴백으로 넘어가지 않았다"


def test_rate_limit_does_not_burn_the_fallback(monkeypatch) -> None:
    """429 는 모델이 아니라 우리 속도 문제다 — 같은 모델로 물러섰다 다시 시도한다."""
    ledger = make_ledger()
    _no_wait(monkeypatch, ledger)
    primary = ModelConfig().model
    backend = FlakyBackend({primary: "Client error '429 Too Many Requests'"})
    gw = ModelGateway(backend=backend, ledger=ledger, limiter=RateLimiter())

    with pytest.raises(Exception):
        gw.complete(agent_id="main", messages=[{"role": "user", "content": "x"}])
    assert set(backend.seen) == {primary}, "429 에 폴백을 쓰면 폴백까지 같이 막힌다"
    assert gw.limiter.interval > RateLimiter().interval, "간격을 벌리지 않았다"


def test_every_call_records_which_model_answered(monkeypatch) -> None:
    """어떤 모델이 응답했는지 남기지 않으면 측정이 거짓말이 된다."""
    from api_doctor.model.gateway import DEFAULT_FALLBACK_MODELS

    ledger = make_ledger()
    _no_wait(monkeypatch, ledger)
    primary = ModelConfig().model
    backend = FlakyBackend({primary: "503 Service temporarily overloaded"})
    gw = ModelGateway(backend=backend, ledger=ledger, limiter=RateLimiter.unpaced())
    gw.complete(agent_id="main", messages=[{"role": "user", "content": "x"}])

    assert [c.model for c in gw.calls] == [primary, DEFAULT_FALLBACK_MODELS[0]]
    assert all(c.to_json()["model"] for c in gw.calls), "사건에도 실려야 한다"


def test_fallback_list_excludes_models_that_are_not_callable() -> None:
    """목록에 있다고 부를 수 있는 것은 아니다.

    실측(2026-09-28): llama-3.1-nemotron 계열 3종은 /v1/models 에 나오지만
    /v1/chat/completions 로는 8/8 이 404 였다.
    """
    from api_doctor.model.gateway import DEFAULT_FALLBACK_MODELS

    assert DEFAULT_FALLBACK_MODELS, "폴백이 하나는 있어야 503 에 대응할 수 있다"
    assert not any("llama-3.1-nemotron" in m for m in DEFAULT_FALLBACK_MODELS)


def test_fallback_is_sticky_across_calls(monkeypatch) -> None:
    """주 모델이 죽어 있는 동안 호출마다 503 을 다시 물지 않는다.

    실측: 폴백을 넣고도 호출 21회에 공급자 실패 22회였다. complete() 마다
    주 모델부터 다시 시도해 매번 한 번씩 버리고 있었다.
    """
    from api_doctor.model.gateway import DEFAULT_FALLBACK_MODELS

    ledger = make_ledger()
    _no_wait(monkeypatch, ledger)
    primary = ModelConfig().model
    backend = FlakyBackend({primary: "503 Service temporarily overloaded"})
    gw = ModelGateway(backend=backend, ledger=ledger, limiter=RateLimiter.unpaced())

    for _ in range(3):
        gw.complete(agent_id="main", messages=[{"role": "user", "content": "x"}])

    assert backend.seen.count(primary) == 1, (
        f"주 모델을 {backend.seen.count(primary)}번 다시 시도했다"
    )
    assert backend.seen[1:] == [DEFAULT_FALLBACK_MODELS[0]] * 3


def test_primary_is_rechecked_so_recovery_is_not_missed(monkeypatch) -> None:
    """주 모델이 회복했는데 계속 폴백에 머무르면 더 약한 모델로 평가하게 된다."""
    from api_doctor.model.gateway import PRIMARY_RECHECK_EVERY

    ledger = make_ledger()
    _no_wait(monkeypatch, ledger)
    primary = ModelConfig().model
    backend = FlakyBackend({primary: "503 Service temporarily overloaded"})
    gw = ModelGateway(backend=backend, ledger=ledger, limiter=RateLimiter.unpaced())

    for _ in range(PRIMARY_RECHECK_EVERY + 2):
        gw.complete(agent_id="main", messages=[{"role": "user", "content": "x"}])

    assert backend.seen.count(primary) >= 2, "주 모델을 다시 시험하지 않았다"
