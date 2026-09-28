"""모델 게이트웨이 (FR-003, AC-16).

모든 LLM 호출의 **단일 통로**다. 콜백이 아니라 `BaseChatModel` 서브클래스로
감싸는 이유는, LangChain 내부의 구조화 출력 재시도·자동 요약까지 잡아야 하기
때문이다. 하위 클라이언트는 `max_retries=0` 으로 두고 재시도를 우리가 센다.

이 모듈은 `agents` · `tools` · `verify` · `sandbox` 를 import 하지 않는다.
예산 원장 외에는 아무것도 모른다.
"""

from __future__ import annotations

import os
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
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


class ModelUnavailable(RuntimeError):
    """모델 경로를 사용할 수 없습니다."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str = DEFAULT_MODEL
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

    def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int, timeout: float
    ) -> tuple[str, dict[str, int] | None]:
        """(본문, usage) 를 반환한다. usage 가 없으면 None."""
        ...


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """보수적 토큰 추정.

    실제 토크나이저는 Phase 0 에서 고정한다. 그때까지는 **과대** 추정해
    예산을 넘기지 않는 쪽으로 틀린다. 한국어는 문자당 토큰이 더 나오므로
    영문 기준 4자/토큰이 아니라 2자/토큰으로 잡는다.
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, chars // 2) + 8 * len(messages)


@dataclass(slots=True)
class ModelGateway:
    """예산을 강제하며 모델을 호출한다."""

    backend: ChatBackend
    ledger: BudgetLedger
    config: ModelConfig = field(default_factory=ModelConfig)
    calls: list[ModelCall] = field(default_factory=list)
    on_call: Callable[[ModelCall], None] | None = None

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

        text, error = self._attempt(agent_id, messages, retry_of=None)
        if error is None:
            return text

        if not allow_retry or not _is_transient(error):
            raise ModelUnavailable(error)

        # 재시도 전에 물러선다. 같은 간격으로 즉시 다시 던지면 같은 503 이 온다.
        # 공급자 실패는 호출 예산에서 환불되므로(§refund_failed_call) 여기서
        # 몇 번 더 시도해도 조사 예산을 태우지 않는다. 실질 한계는 시간 예산이다.
        for retry_index in range(MAX_TRANSIENT_RETRIES):
            if self.ledger.provider_failures_exhausted:
                raise ModelUnavailable(
                    f"공급자 실패가 {self.ledger.failed_attempts}회 누적되어 "
                    f"중단합니다: {error}"
                )
            delay = min(
                3.0 * (retry_index + 1),
                max(0.0, self.ledger.usable_seconds - self.config.timeout_seconds),
            )
            if delay > 0:
                self.ledger.cancel.wait(delay)
            self.ledger.cancel.raise_if_cancelled()

            text, error = self._attempt(
                agent_id, messages, retry_of=len(self.calls)
            )
            if error is None:
                return text
            if not _is_transient(error):
                break

        raise ModelUnavailable(error)

    def _attempt(
        self, agent_id: str, messages: list[dict[str, Any]], *, retry_of: int | None
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
        self, messages: list[dict[str, Any]], *, max_tokens: int, timeout: float
    ) -> tuple[str, dict[str, int] | None]:
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self.config.model,
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

    responses: list[str]
    usage_per_call: dict[str, int] | None = None
    _index: int = 0

    def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int, timeout: float
    ) -> tuple[str, dict[str, int] | None]:
        if self._index >= len(self.responses):
            raise RuntimeError("scripted 응답이 소진되었습니다.")
        text = self.responses[self._index]
        self._index += 1
        return text, self.usage_per_call
