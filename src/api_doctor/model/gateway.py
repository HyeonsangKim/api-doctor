"""모델 게이트웨이 (FR-003, AC-16).

모든 LLM 호출의 **단일 통로**다. 콜백이 아니라 `BaseChatModel` 서브클래스로
감싸는 이유는, LangChain 내부의 구조화 출력 재시도·자동 요약까지 잡아야 하기
때문이다. 하위 클라이언트는 `max_retries=0` 으로 두고 재시도를 우리가 센다.

이 모듈은 `agents` · `tools` · `verify` · `sandbox` 를 import 하지 않는다.
예산 원장 외에는 아무것도 모른다.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

from ..runtime.budget import BudgetExceeded, BudgetLedger, Cancelled

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# 일시 오류 재시도 횟수. 실패는 환불되므로 시간 예산이 실질 한계다.
#
# 실측(2026-09-28): 한 run 에 503 이 8회까지 났다. 3회로는 한 호출이
# 끝내 실패해 파이프라인 단계가 통째로 날아간다. 실행이 100~300초이고
# 시간 예산이 600초라 여유가 있다.
MAX_TRANSIENT_RETRIES = 5
#: 호출 사이 최소 간격. 무료 등급의 초당 요청 한도를 넘지 않기 위한 것이다.
MIN_CALL_INTERVAL = 1.5
#: 429 가 반복될 때 간격 상한.
MAX_CALL_INTERVAL = 20.0
#: 이만큼 연속 성공하면 간격을 줄여 본다.
REWARD_STREAK = 4
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
#: 주 모델이 과부하일 때 대신 쓸 모델.
#:
#: 실측(2026-09-28, 무료 등급, 각 8회):
#:   nemotron-3-super-120b-a12b            1/8  (429×4, 503×3)
#:   nemotron-3-nano-omni-30b-a3b-reasoning 4/8 (503×4)
#:   llama-3.1-nemotron 계열 3종            0/8  (404 — 목록엔 있으나 호출 불가)
#:
#: 503 "Service temporarily overloaded" 는 우리가 만든 부하가 아니므로
#: 간격을 벌려도 줄지 않는다. 다른 모델로 넘어가는 것만이 답이다.
#: 어떤 모델이 응답했는지는 호출마다 기록한다 — 그러지 않으면 측정이 거짓말이 된다.
DEFAULT_FALLBACK_MODELS = ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",)


class ModelUnavailable(RuntimeError):
    """모델 경로를 사용할 수 없습니다."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str = DEFAULT_MODEL
    fallback_models: tuple[str, ...] = DEFAULT_FALLBACK_MODELS
    base_url: str = NVIDIA_BASE_URL
    temperature: float = 0.0
    timeout_seconds: float = 45.0
    api_key_env: str = "NVIDIA_API_KEY"

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass(slots=True)
class ModelCall:
    """호출 1건의 계측. `model_call` 사건이 된다 (아키텍처 §8.2)."""

    agent_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    tokens_kind: str
    #: 실제로 응답한 모델. 폴백이 쓰였으면 주 모델과 다르다.
    model: str = ""
    # 추론 모델의 관측용. 예산은 completion_tokens 로 정산한다.
    reasoning_chars: int = 0
    finish_reason: str = ""
    error: str | None = None
    retry_of: int | None = None
    # run 시작 기준 상대 시각(ms). Gantt 와 구간 대조에 쓴다 (FR-018).
    started_ms: int = 0
    ended_ms: int = 0

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "tokens_kind": self.tokens_kind,
            "reasoning_chars": self.reasoning_chars,
            "finish_reason": self.finish_reason,
            "error": self.error,
            "retry_of": self.retry_of,
            "started_ms": self.started_ms,
            "ended_ms": self.ended_ms,
        }


class ChatBackend(Protocol):
    """게이트웨이가 감싸는 실제 모델 클라이언트."""

    #: 공급자에게 실제 요청이 나가는가. 레이트리밋은 여기에만 걸린다 —
    #: scripted backend 까지 재우면 테스트가 느려질 뿐 아무것도 지키지 못한다.
    is_remote: bool

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        timeout: float,
        model: str | None = None,
    ) -> tuple[str, dict[str, int] | None]:
        """(본문, usage) 를 반환한다. usage 가 없으면 None.

        `model` 이 주어지면 그 모델로 부른다 — 주 모델이 과부하일 때
        게이트웨이가 폴백 모델을 지정한다.
        """
        ...


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """보수적 토큰 추정.

    실제 토크나이저는 Phase 0 에서 고정한다. 그때까지는 **과대** 추정해
    예산을 넘기지 않는 쪽으로 틀린다. 한국어는 문자당 토큰이 더 나오므로
    영문 기준 4자/토큰이 아니라 2자/토큰으로 잡는다.
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, chars // 2) + 8 * len(messages)


class RateLimiter:
    """호출 사이에 최소 간격을 둔다 — 429 를 맞고 물러서는 대신 안 맞는다.

    실측에서 429 가 시도의 73% 였다. 그건 공급자 장애가 아니라 우리가
    무료 등급의 초당 요청 한도를 넘긴 것이다. 맞고 나서 backoff 하면
    이미 전역 실패 허용량을 태운 뒤라 뒤에 오는 역할(수리자)이 굶는다.

    간격은 적응적이다. 429 를 맞으면 늘리고, 연속 성공하면 천천히 줄인다.
    게이트웨이 하나가 모든 역할의 호출을 통과시키므로 이 한 곳이면 된다.
    """

    __slots__ = ("_interval", "_last", "_streak", "_min", "_max", "_now")

    def __init__(
        self,
        *,
        initial: float = MIN_CALL_INTERVAL,
        minimum: float = MIN_CALL_INTERVAL,
        maximum: float = MAX_CALL_INTERVAL,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._interval = initial
        self._min = minimum
        self._max = maximum
        self._last: float | None = None
        self._streak = 0
        self._now = now or time.monotonic

    @classmethod
    def unpaced(cls) -> "RateLimiter":
        """간격을 두지 않는다. 공급자에게 나가지 않는 backend 용."""
        return cls(initial=0.0, minimum=0.0, maximum=0.0)

    @property
    def interval(self) -> float:
        return self._interval

    def due(self) -> float:
        """지금 부르기 전에 기다려야 할 초. 기다림 자체는 호출자가 한다.

        게이트웨이는 취소 토큰으로 기다려야 하므로 여기서 자지 않는다.
        """
        if self._last is None:
            return 0.0
        return max(0.0, self._interval - (self._now() - self._last))

    def mark(self) -> None:
        """호출을 보냈다고 기록한다."""
        self._last = self._now()

    def penalize(self, retry_after: float | None = None) -> float:
        """429 를 맞았다. 간격을 늘리고 물러설 시간을 돌려준다."""
        self._streak = 0
        self._interval = min(self._max, max(self._interval * 2.0, self._min * 2.0))
        # 공급자가 명시한 Retry-After 가 우리 추정보다 우선한다.
        return max(self._interval, retry_after or 0.0)

    def reward(self) -> None:
        """연속 성공이 쌓이면 조심스럽게 간격을 줄인다."""
        self._streak += 1
        if self._streak >= REWARD_STREAK:
            self._streak = 0
            self._interval = max(self._min, self._interval * 0.75)


def parse_retry_after(error: str) -> float | None:
    """오류 문자열에 실린 Retry-After 초를 읽는다."""
    m = re.search(r"retry[- ]after[\"'\s:=]+(\d+(?:\.\d+)?)", error, re.IGNORECASE)
    if m is None:
        return None
    try:
        return min(float(m.group(1)), MAX_CALL_INTERVAL)
    except ValueError:
        return None


def is_rate_limited(error: str) -> bool:
    """공급자 장애가 아니라 우리가 너무 빨리 부른 경우인가."""
    return "429" in error or "too many requests" in error.lower()


@dataclass(slots=True)
class ModelGateway:
    """예산을 강제하며 모델을 호출한다."""

    backend: ChatBackend
    ledger: BudgetLedger
    config: ModelConfig = field(default_factory=ModelConfig)
    calls: list[ModelCall] = field(default_factory=list)
    on_call: Callable[[ModelCall], None] | None = None
    limiter: RateLimiter | None = None

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = (
                RateLimiter()
                if getattr(self.backend, "is_remote", False)
                else RateLimiter.unpaced()
            )

    def complete(
        self,
        *,
        agent_id: str,
        messages: list[dict[str, Any]],
        allow_retry: bool = True,
    ) -> str:
        """1회 추론. 예산 초과·취소는 예외로 올린다.

        일시 오류 재시도는 전부 계측된다 (PRD §4.1). 다만 공급자가 본문도
        usage 도 주지 않은 실패는 모델 작업이 아니므로 호출 허용량을 환불한다.
        """
        self.ledger.cancel.raise_if_cancelled()

        self._pace()
        model = self.config.model
        text, error = self._attempt(agent_id, messages, retry_of=None, model=model)
        if error is None:
            self.limiter.reward()
            return text

        if not allow_retry or not _is_transient(error):
            raise ModelUnavailable(error)

        # 두 가지 실패를 구분해 다르게 대응한다.
        #
        #   429  우리가 너무 빨리 불렀다  → 간격을 벌린다
        #   503  공급자가 과부하다        → 다른 모델로 넘어간다
        #
        # 실측(2026-09-28)에서 주 모델의 성공률이 1/8 이었고 503 은 0.1초 만에
        # 즉시 거절됐다. 우리가 만든 부하가 아니므로 기다려도 줄지 않는다.
        # 호출 예산은 환불되므로(§refund_failed_call) 재시도가 조사 예산을
        # 태우지 않는다. 실질 한계는 시간 예산이다.
        fallbacks = iter(self.config.fallback_models)
        for retry_index in range(MAX_TRANSIENT_RETRIES):
            if self.ledger.provider_failures_exhausted_for(agent_id):
                raise ModelUnavailable(
                    f"공급자 실패가 {self.ledger.failed_attempts}회 누적되어 "
                    f"{agent_id} 의 재시도를 중단합니다: {error}"
                )
            if is_rate_limited(error):
                wanted = self.limiter.penalize(parse_retry_after(error))
            else:
                # 과부하다. 남은 폴백이 있으면 기다리지 말고 갈아탄다.
                nxt = next(fallbacks, None)
                if nxt is not None:
                    model = nxt
                    wanted = 0.0
                else:
                    wanted = 3.0 * (retry_index + 1)
            delay = min(
                wanted,
                max(0.0, self.ledger.usable_seconds - self.config.timeout_seconds),
            )
            if delay > 0:
                self.ledger.cancel.wait(delay)
            self.ledger.cancel.raise_if_cancelled()
            self.limiter.mark()

            text, error = self._attempt(
                agent_id, messages, retry_of=len(self.calls), model=model
            )
            if error is None:
                self.limiter.reward()
                return text
            if not _is_transient(error):
                break

        raise ModelUnavailable(error)

    def _pace(self) -> None:
        """최소 간격이 지날 때까지 취소 가능하게 기다린다."""
        delay = self.limiter.due()
        if delay > 0:
            self.ledger.cancel.wait(min(delay, max(0.0, self.ledger.usable_seconds)))
            self.ledger.cancel.raise_if_cancelled()
        self.limiter.mark()

    def _attempt(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        *,
        retry_of: int | None,
        model: str | None = None,
    ) -> tuple[str, str | None]:
        estimated = estimate_tokens(messages)
        reservation = self.ledger.reserve_model_call(agent_id, estimated)
        max_output = self.ledger.limits.max_output_tokens.get(agent_id, 2048)

        started = time.monotonic()
        started_ms = int(self.ledger.elapsed * 1000)
        text, usage, error = "", None, None
        try:
            text, usage = self.backend.complete(
                messages,
                model=model,
                max_tokens=max_output,
                timeout=min(
                    self.config.timeout_seconds, max(1.0, self.ledger.usable_seconds)
                ),
            )
        except Cancelled:
            self.ledger.settle_model_call(reservation, usage_tokens=None)
            raise
        except Exception as exc:  # noqa: BLE001 - 공급자 오류를 구조화해 계측한다
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = int((time.monotonic() - started) * 1000)

        # 공급자가 본문도 usage 도 주지 않은 실패는 모델 작업이 아니다.
        # 원장에 시도로 남기되 호출 허용량은 돌려준다.
        if error is not None and usage is None:
            self.ledger.refund_failed_call(reservation)
            call = ModelCall(
                agent_id=agent_id, input_tokens=None, output_tokens=None,
                latency_ms=latency_ms, tokens_kind="failed", error=error,
                model=model or self.config.model,
                retry_of=retry_of, started_ms=started_ms,
                ended_ms=int(self.ledger.elapsed * 1000),
            )
            self.calls.append(call)
            if self.on_call is not None:
                self.on_call(call)
            return text, error

        total = None
        if usage is not None:
            total = int(usage.get("total_tokens") or
                        (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)))
            reasoning_chars = int(usage.get("reasoning_chars", 0))
        self.ledger.settle_model_call(reservation, usage_tokens=total)

        call = ModelCall(
            agent_id=agent_id,
            model=model or self.config.model,
            input_tokens=(usage or {}).get("input_tokens"),
            output_tokens=(usage or {}).get("output_tokens"),
            latency_ms=latency_ms,
            reasoning_chars=int((usage or {}).get("reasoning_chars", 0)),
            finish_reason=str((usage or {}).get("finish_reason", "")),
            tokens_kind="actual" if usage is not None else "estimated",
            error=error,
            retry_of=retry_of,
            started_ms=started_ms,
            ended_ms=int(self.ledger.elapsed * 1000),
        )
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)
        return text, error

    # ------------------------------------------------------------------ 대조

    def per_role_calls(self) -> dict[str, int]:
        """AC-16: 역할별 **과금된** 호출 수. 원장과 일치해야 한다.

        공급자가 본문도 usage 도 주지 않은 시도는 환불되므로 여기서 뺀다.
        그러지 않으면 프로파일과 원장이 어긋나 대조가 항상 실패한다.
        시도 총량은 `per_role_attempts()` 로 따로 본다.
        """
        counts: dict[str, int] = {}
        for call in self.calls:
            if call.tokens_kind == "failed":
                continue
            counts[call.agent_id] = counts.get(call.agent_id, 0) + 1
        return counts

    def per_role_attempts(self) -> dict[str, int]:
        """공급자 실패를 포함한 시도 수. 관측용이다."""
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.agent_id] = counts.get(call.agent_id, 0) + 1
        return counts

    def reconcile(self, profiler_counts: dict[str, int]) -> dict[str, Any]:
        """프로파일러와 원장을 대조한다. 다르면 차이를 보고하고 원장을 따른다."""
        ledger_counts = self.per_role_calls()
        roles = sorted(set(ledger_counts) | set(profiler_counts))
        diff = {
            role: {"ledger": ledger_counts.get(role, 0),
                   "profiler": profiler_counts.get(role, 0)}
            for role in roles
            if ledger_counts.get(role, 0) != profiler_counts.get(role, 0)
        }
        return {
            "matches": not diff,
            "ledger_total": sum(ledger_counts.values()),
            "profiler_total": sum(profiler_counts.values()),
            "differences": diff,
            "authority": "gateway_ledger",
        }


_TRANSIENT = ("timeout", "timed out", "503", "502", "429", "connection", "temporarily")


def _is_transient(error: str) -> bool:
    """영구 오류·무료 한도 초과에는 재시도하지 않는다 (PRD §4.1)."""
    lowered = error.lower()
    if any(m in lowered for m in ("quota", "insufficient", "payment", "401", "403")):
        return False
    return any(m in lowered for m in _TRANSIENT)


# ----------------------------------------------------------- 실제 backend


class NvidiaChatBackend:
    """NVIDIA hosted endpoint (OpenAI 호환).

    `max_retries=0` 이다. 재시도는 게이트웨이가 계측하며 센다.
    """

    is_remote = True

    def __init__(self, config: ModelConfig, cancel_token: Any = None) -> None:
        key = config.api_key()
        if not key:
            raise ModelUnavailable(
                f"{config.api_key_env} 가 설정되지 않았습니다. "
                "build.nvidia.com 에서 무료 키를 발급하세요."
            )
        import httpx

        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=config.timeout_seconds,
        )
        if cancel_token is not None:
            cancel_token.on_cancel(self._client.close)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        timeout: float,
        model: str | None = None,
    ) -> tuple[str, dict[str, int] | None]:
        response = self._client.post(
            "/chat/completions",
            json={
                "model": model or self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish = str(choice.get("finish_reason", ""))

        # 이 모델은 reasoning 모델이다. 추론은 `reasoning_content` 로 따로 오고
        # `completion_tokens` 에 함께 계산된다. 추론이 max_tokens 를 다 쓰면
        # content 가 비어 온다 — 빈 문자열을 정상 응답처럼 돌려주면
        # 상위에서 "형식 위반" 으로 오인하므로 여기서 구분해 올린다.
        reasoning = message.get("reasoning_content") or ""

        # 잘린 응답은 정상 응답이 아니다. 그대로 올리면 상위에서
        # "형식 위반" 으로 오인하고 조사가 조용히 끝난다.
        if finish == "length":
            raise ModelUnavailable(
                f"출력이 상한 {max_tokens} 토큰에서 잘렸습니다 "
                f"(본문 {len(text)}자, 추론 {len(reasoning)}자). "
                "출력 상한을 올리거나 더 짧게 쓰도록 지시해야 합니다."
            )
        if not text.strip() and reasoning:
            raise ModelUnavailable(
                "모델이 추론만 반환하고 본문을 내지 않았습니다."
            )

        raw = data.get("usage") or {}
        usage = (
            {
                "input_tokens": int(raw.get("prompt_tokens", 0)),
                "output_tokens": int(raw.get("completion_tokens", 0)),
                "total_tokens": int(raw.get("total_tokens", 0)),
                # 관측용. 예산은 completion_tokens 로 정산한다.
                "reasoning_chars": len(reasoning),
                "finish_reason": finish,
            }
            if raw
            else None
        )
        return text, usage


@dataclass(slots=True)
class ScriptedBackend:
    """테스트용 고정 응답 backend.

    PRD §7.2 가 요구하는 대로, 이 backend 의 결과는 **scripted** 이며
    실제 모델 trace 와 구분해 표기한다.
    """

    is_remote = False

    responses: list[str]
    usage_per_call: dict[str, int] | None = None
    _index: int = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        timeout: float,
        model: str | None = None,
    ) -> tuple[str, dict[str, int] | None]:
        if self._index >= len(self.responses):
            raise RuntimeError("scripted 응답이 소진되었습니다.")
        text = self.responses[self._index]
        self._index += 1
        return text, self.usage_per_call
