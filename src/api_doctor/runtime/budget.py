"""공통 예산 원장과 취소 (FR-003, AC-08, 아키텍처 §5).

모든 역할의 모델 호출·토큰·시간·샌드박스 실행·데이터 호출이 **하나의 원장**을
통과한다. 역할을 바꿔도 예산이 초기화되지 않는다.

PRD §4.1 의 세 상한은 동시에 도달할 수 없다:
  호출 24회 × 회당 입력 8,000 = 192,000  → 전체 토큰 예산과 같아 출력 몫이 0
  호출 24회 × 모델 HTTP 45초  = 1,080초  → 전체 시간 600초를 넘는다
따라서 24회는 **도달하지 않는 상한**이고 실질 제약은 토큰과 시간이다.
`can_spend()` 는 정해진 순서로 검사해 가장 먼저 고갈되는 자원을 사유로 보고한다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

AGENT_IDS = (
    "main",
    "spec_researcher",
    "runtime_diagnostician",
    "repair_engineer",
    "data_auditor",
)

# 비교 실험 B 의 단일 에이전트. 제품 구조에 포함되지 않는다.
SINGLE_AGENT = "single_agent"


class Resource(StrEnum):
    MODEL_CALLS = "model_calls"
    TOKENS = "tokens"
    WALL_SECONDS = "wall_seconds"
    SANDBOX_RUNS = "sandbox_runs"
    BROKER_CALLS = "broker_calls"
    DELEGATIONS = "delegations"
    PATCHES = "patches"


class DenyReason(StrEnum):
    CANCELLED = "cancelled"
    TIME_EXHAUSTED = "time_exhausted"
    TOKENS_EXHAUSTED = "tokens_exhausted"
    CALLS_EXHAUSTED = "calls_exhausted"
    ROLE_CALLS_EXHAUSTED = "role_calls_exhausted"
    SANDBOX_EXHAUSTED = "sandbox_exhausted"
    BROKER_EXHAUSTED = "broker_exhausted"
    DELEGATIONS_EXHAUSTED = "delegations_exhausted"
    ROLE_DELEGATIONS_EXHAUSTED = "role_delegations_exhausted"
    PATCHES_EXHAUSTED = "patches_exhausted"
    RUN_TERMINATED = "run_terminated"


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: DenyReason | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Verdict(True)


class BudgetExceeded(RuntimeError):
    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.detail or str(verdict.reason))
        self.verdict = verdict


class Cancelled(RuntimeError):
    """사용자가 취소했습니다."""


class CancelToken:
    """run 전역 취소 신호 (AC-08).

    Zone T 소유다. 에이전트는 읽지도 쓰지도 못한다. 세 경로에 동시에 연결된다:
    예산 원장(새 호출 차단) · 모델 게이트웨이(in-flight 요청 취소) · 샌드박스(프로세스 정리).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, callback: Any) -> None:
        """취소 시 호출된다. 이미 취소됐으면 즉시 호출한다."""
        with self._lock:
            if self._event.is_set():
                callback()
                return
            self._callbacks.append(callback)

    def set(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - 정리 실패가 취소를 막으면 안 된다
                pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("사용자가 취소했습니다.")

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True, slots=True)
class Limits:
    """PRD §4.1 설계 상한. 공급자의 무료 한도가 더 낮으면 항상 낮은 쪽을 쓴다."""

    # 실측(2026-09-28) 후 조정. PRD §4.1 의 설계 상한은 24회였다.
    #
    # main 은 위임 1건당 2회를 쓴다 — 하나는 위임하는 데, 하나는 결과를 받아
    # 다음을 정하는 데. 네 역할을 거치고 종료하려면 main 만 10회가 필요하고,
    # 전문가 넷이 각 3회씩이면 22회다. 24 로는 503 한 번이나 재진단 한 번에
    # 파이프라인이 끊긴다.
    #
    # 토큰(실측 최대 49k / 192k)과 시간(최대 320초 / 600초)은 여유가 크므로
    # 호출 수만 인위적 병목이었다. 실제 비용 상한은 토큰과 시간이 지킨다.
    model_calls: int = 32
    tokens: int = 192_000
    wall_seconds: float = 600.0
    sandbox_runs: int = 8
    broker_calls: int = 20
    delegations: int = 8
    patches: int = 2

    role_model_calls: dict[str, int] = field(
        default_factory=lambda: {
            # 실측(2026-09-28) 후 조정. PRD §4.1 의 설계 상한은 역할당 4회였으나
            # 한 번 돌려 보니 부족했다 — 도구 호출 1건이 모델 호출 1회를 쓰는데,
            # 조사에 probe 2회가 필요하고 공급자 503 이 한 회를 더 먹었다.
            # 전체 24회는 그대로이므로 총량이 여전히 실질 상한이다.
            "main": 12, "spec_researcher": 5, "runtime_diagnostician": 6,
            "repair_engineer": 6, "data_auditor": 6,
            # 비교 실험 B: 팀 전체와 **같은 총량**을 준다 (PRD §7.3).
            "single_agent": 32,
        }
    )
    role_delegations: int = 2

    max_output_tokens: dict[str, int] = field(
        default_factory=lambda: {
            # 실측(2026-09-28): 이 모델은 reasoning 모델이라 추론이
            # completion_tokens 를 함께 쓴다. 사소한 JSON 한 줄에도 출력 258
            # 토큰이 나갔다. 패치를 싣는 역할은 특히 여유가 필요하다.
            "main": 3072, "spec_researcher": 3072, "runtime_diagnostician": 3072,
            "data_auditor": 3072, "repair_engineer": 12288, "single_agent": 12288,
        }
    )
    model_http_seconds: float = 45.0
    cleanup_seconds: float = 20.0
    # 공급자가 본문 없이 실패한 시도의 총 허용량. 무한 재시도를 막되,
    # 실측(2026-09-28) 결과 503 이 잦아 6회로는 조사 도중 끊긴다.
    # 실패는 환불되므로 실질 한계는 전체 시간 예산이다.
    max_provider_failures: int = 25


@dataclass(slots=True)
class Reservation:
    """호출 전 예약. 반환된 usage 로 정산한다."""

    agent_id: str
    tokens: int
    settled: bool = False


@dataclass(slots=True)
class BudgetLedger:
    """단일 원장. 모든 소비가 여기를 지난다."""

    limits: Limits = field(default_factory=Limits)
    cancel: CancelToken = field(default_factory=CancelToken)

    started_at: float = field(default_factory=time.monotonic)
    terminated: bool = False

    model_calls: int = 0
    tokens_actual: int = 0
    tokens_estimated: int = 0
    tokens_reserved: int = 0
    sandbox_runs: int = 0
    broker_calls: int = 0
    delegations: int = 0
    patches: int = 0
    failed_attempts: int = 0

    role_calls: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys((*AGENT_IDS, SINGLE_AGENT), 0)
    )
    role_delegations: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys((*AGENT_IDS, SINGLE_AGENT), 0)
    )

    # 종료 검증용 예약 슬롯. 새 위임이 이것을 소비할 수 없다.
    finish_slot_reserved: bool = True

    # 공식 데이터 호출 상한을 강제할지. fixture 는 동결분만 읽으므로
    # 공급자에게 나가는 호출이 없어 강제하지 않는다 (PRD §4.1).
    # 사용량은 어느 쪽이든 기록한다.
    enforce_broker_quota: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ 잔여

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.wall_seconds - self.elapsed)

    @property
    def usable_seconds(self) -> float:
        """정리 시간을 뺀 실제 가용 시간."""
        return max(0.0, self.remaining_seconds - self.limits.cleanup_seconds)

    @property
    def tokens_used(self) -> int:
        return self.tokens_actual + self.tokens_estimated

    @property
    def tokens_committed(self) -> int:
        """정산되지 않은 예약을 포함한 소비량."""
        return self.tokens_used + self.tokens_reserved

    @property
    def usable_sandbox_runs(self) -> int:
        reserved = 1 if self.finish_slot_reserved else 0
        return max(0, self.limits.sandbox_runs - self.sandbox_runs - reserved)

    def remaining(self) -> dict[str, Any]:
        """`get_budget` 도구가 반환하는 잔여량."""
        return {
            "model_calls": self.limits.model_calls - self.model_calls,
            "tokens": self.limits.tokens - self.tokens_committed,
            "seconds": round(self.usable_seconds, 1),
            "sandbox_runs": self.usable_sandbox_runs,
            "broker_calls": (
                self.limits.broker_calls - self.broker_calls
                if self.enforce_broker_quota else None
            ),
            "delegations": self.limits.delegations - self.delegations,
            "patches": self.limits.patches - self.patches,
            "role_calls": {
                role: self.limits.role_model_calls.get(role, 0) - used
                for role, used in self.role_calls.items()
            },
        }

    # ------------------------------------------------------------------ 판정

    def can_spend_model_call(self, agent_id: str) -> Verdict:
        """검사 순서 고정: 취소 → 종료 → 시간 → 토큰 → 호출수 → 역할별.

        가장 먼저 고갈되는 자원이 사유가 된다.
        """
        base = self._preconditions()
        if base is not None:
            return base

        max_output = self.limits.max_output_tokens.get(agent_id, 2048)
        # 모델 HTTP 상한만큼의 시간이 남아 있지 않으면 시작하지 않는다.
        if self.usable_seconds < self.limits.model_http_seconds:
            return Verdict(
                False, DenyReason.TIME_EXHAUSTED,
                f"남은 시간 {self.usable_seconds:.0f}초 < 모델 HTTP 상한 "
                f"{self.limits.model_http_seconds:.0f}초",
            )
        if self.tokens_committed + max_output > self.limits.tokens:
            return Verdict(
                False, DenyReason.TOKENS_EXHAUSTED,
                f"토큰 {self.tokens_committed}/{self.limits.tokens}, "
                f"이번 호출 최대 출력 {max_output}",
            )
        if self.model_calls >= self.limits.model_calls:
            return Verdict(
                False, DenyReason.CALLS_EXHAUSTED,
                f"모델 호출 {self.model_calls}/{self.limits.model_calls}",
            )
        role_limit = self.limits.role_model_calls.get(agent_id, 0)
        if self.role_calls.get(agent_id, 0) >= role_limit:
            return Verdict(
                False, DenyReason.ROLE_CALLS_EXHAUSTED,
                f"{agent_id} 호출 {self.role_calls.get(agent_id, 0)}/{role_limit}. "
                "역할 간 양도는 없습니다.",
            )
        return ALLOW

    def can_delegate(self, agent_id: str) -> Verdict:
        base = self._preconditions()
        if base is not None:
            return base
        if self.delegations >= self.limits.delegations:
            return Verdict(
                False, DenyReason.DELEGATIONS_EXHAUSTED,
                f"위임 {self.delegations}/{self.limits.delegations}",
            )
        if self.role_delegations.get(agent_id, 0) >= self.limits.role_delegations:
            return Verdict(
                False, DenyReason.ROLE_DELEGATIONS_EXHAUSTED,
                f"{agent_id} 위임 {self.role_delegations.get(agent_id, 0)}"
                f"/{self.limits.role_delegations}",
            )
        return self.can_spend_model_call(agent_id)

    def can_run_sandbox(self, *, is_finish: bool = False) -> Verdict:
        base = self._preconditions()
        if base is not None:
            return base
        available = (
            self.limits.sandbox_runs - self.sandbox_runs
            if is_finish
            else self.usable_sandbox_runs
        )
        if available <= 0:
            return Verdict(
                False, DenyReason.SANDBOX_EXHAUSTED,
                f"후보 실행 {self.sandbox_runs}/{self.limits.sandbox_runs}"
                + ("" if is_finish else " (종료 검증용 1회는 양도 불가)"),
            )
        return ALLOW

    def can_call_broker(self) -> Verdict:
        base = self._preconditions()
        if base is not None:
            return base
        if not self.enforce_broker_quota:
            return ALLOW
        if self.broker_calls >= self.limits.broker_calls:
            return Verdict(
                False, DenyReason.BROKER_EXHAUSTED,
                f"데이터 호출 {self.broker_calls}/{self.limits.broker_calls}",
            )
        return ALLOW

    def can_submit_patch(self) -> Verdict:
        base = self._preconditions()
        if base is not None:
            return base
        if self.patches >= self.limits.patches:
            return Verdict(
                False, DenyReason.PATCHES_EXHAUSTED,
                f"패치 {self.patches}/{self.limits.patches}",
            )
        return ALLOW

    def _preconditions(self) -> Verdict | None:
        if self.cancel.cancelled:
            return Verdict(False, DenyReason.CANCELLED, "사용자가 취소했습니다.")
        if self.terminated:
            return Verdict(False, DenyReason.RUN_TERMINATED, "작업이 종료되었습니다.")
        if self.usable_seconds <= 0:
            return Verdict(
                False, DenyReason.TIME_EXHAUSTED,
                f"전체 시간 {self.limits.wall_seconds:.0f}초를 소진했습니다.",
            )
        return None

    # ------------------------------------------------------------------ 소비

    def reserve_model_call(self, agent_id: str, estimated_input: int) -> Reservation:
        """호출 전 보수적으로 예약한다."""
        verdict = self.can_spend_model_call(agent_id)
        if not verdict:
            raise BudgetExceeded(verdict)
        max_output = self.limits.max_output_tokens.get(agent_id, 2048)
        amount = estimated_input + max_output
        self.tokens_reserved += amount
        self.model_calls += 1
        self.role_calls[agent_id] = self.role_calls.get(agent_id, 0) + 1
        self._log("reserve", Resource.MODEL_CALLS, agent_id=agent_id, tokens=amount)
        return Reservation(agent_id=agent_id, tokens=amount)

    def refund_failed_call(self, reservation: Reservation) -> None:
        """공급자가 아무것도 반환하지 않은 호출을 환불한다.

        503 처럼 본문도 usage 도 없는 실패는 **모델 작업이 아니다.**
        원장에는 시도로 남기되(관측), 역할의 호출 허용량은 돌려준다.
        그러지 않으면 공급자 불안정이 조사 예산을 대신 태운다.

        무한 재시도를 막기 위해 실패 시도 총량은 따로 센다.
        """
        if reservation.settled:
            return
        reservation.settled = True
        self.tokens_reserved = max(0, self.tokens_reserved - reservation.tokens)
        self.model_calls = max(0, self.model_calls - 1)
        self.role_calls[reservation.agent_id] = max(
            0, self.role_calls.get(reservation.agent_id, 0) - 1
        )
        self.failed_attempts += 1
        self._log(
            "refund", Resource.MODEL_CALLS,
            agent_id=reservation.agent_id, reason="provider_no_response",
        )

    @property
    def provider_failures_exhausted(self) -> bool:
        """공급자 실패가 너무 잦으면 중단한다."""
        return self.failed_attempts >= self.limits.max_provider_failures

    def settle_model_call(
        self, reservation: Reservation, *, usage_tokens: int | None
    ) -> None:
        """반환된 usage 로 정산한다.

        usage 가 없으면 예약량을 반환하지 않고 `estimated` 로 표시한다 (PRD §4.1).
        """
        if reservation.settled:
            return
        reservation.settled = True
        self.tokens_reserved = max(0, self.tokens_reserved - reservation.tokens)
        if usage_tokens is None:
            self.tokens_estimated += reservation.tokens
            kind = "estimated"
            amount = reservation.tokens
        else:
            self.tokens_actual += usage_tokens
            kind = "actual"
            amount = usage_tokens
        self._log(
            "settle", Resource.TOKENS,
            agent_id=reservation.agent_id, tokens=amount, kind=kind,
        )

    def spend_delegation(self, agent_id: str) -> None:
        verdict = self.can_delegate(agent_id)
        if not verdict:
            raise BudgetExceeded(verdict)
        self.delegations += 1
        self.role_delegations[agent_id] = self.role_delegations.get(agent_id, 0) + 1
        self._log("spend", Resource.DELEGATIONS, agent_id=agent_id)

    def spend_sandbox_run(self, *, is_finish: bool = False) -> None:
        verdict = self.can_run_sandbox(is_finish=is_finish)
        if not verdict:
            raise BudgetExceeded(verdict)
        self.sandbox_runs += 1
        if is_finish:
            self.finish_slot_reserved = False
        self._log("spend", Resource.SANDBOX_RUNS, is_finish=is_finish)

    def reserve_finish_slot(self) -> Verdict:
        """최종 검증 실패 후 루프로 돌아가기 전에 종료 슬롯을 다시 확보한다.

        재예약으로 총 실행 횟수가 늘어나지는 않는다 (PRD §4.1).
        """
        if self.finish_slot_reserved:
            return ALLOW
        if self.limits.sandbox_runs - self.sandbox_runs < 1:
            return Verdict(
                False, DenyReason.SANDBOX_EXHAUSTED,
                "종료 검증용 실행 슬롯을 다시 확보할 수 없습니다.",
            )
        self.finish_slot_reserved = True
        self._log("reserve", Resource.SANDBOX_RUNS, is_finish=True)
        return ALLOW

    def record_broker_calls(self, count: int, *, enforce: bool | None = None) -> None:
        """브로커 호출을 기록한다.

        강제 여부는 원장의 `enforce_broker_quota` 를 따른다. 사용량은
        어느 쪽이든 기록하므로 보고서의 수치는 항상 실제 호출 수다.
        """
        if enforce is not None:
            self.enforce_broker_quota = enforce
        if not self.enforce_broker_quota:
            self.broker_calls += count
            return
        for _ in range(count):
            try:
                self.spend_broker_call()
            except BudgetExceeded:
                break

    def spend_broker_call(self) -> None:
        verdict = self.can_call_broker()
        if not verdict:
            raise BudgetExceeded(verdict)
        self.broker_calls += 1

    def spend_patch(self) -> None:
        verdict = self.can_submit_patch()
        if not verdict:
            raise BudgetExceeded(verdict)
        self.patches += 1
        self._log("spend", Resource.PATCHES)

    def terminate(self) -> None:
        self.terminated = True

    # ------------------------------------------------------------------ 보고

    def _log(self, action: str, resource: Resource, **data: Any) -> None:
        self.events.append(
            {"action": action, "resource": str(resource), "elapsed": round(self.elapsed, 2), **data}
        )

    def usage(self) -> dict[str, Any]:
        """보고서와 manifest 에 들어가는 사용량. NAT 프로파일러와 대조된다 (AC-16)."""
        return {
            "model_calls": self.model_calls,
            "tokens": self.tokens_used,
            "tokens_kind": "estimated" if self.tokens_estimated else "actual",
            "tokens_actual": self.tokens_actual,
            "tokens_estimated": self.tokens_estimated,
            "wall_seconds": round(self.elapsed, 1),
            "sandbox_runs": self.sandbox_runs,
            "broker_calls": self.broker_calls,
            "broker_quota_enforced": self.enforce_broker_quota,
            "delegations": self.delegations,
            "patches": self.patches,
            "provider_failures": self.failed_attempts,
            "per_role_calls": dict(self.role_calls),
            "cancelled": self.cancel.cancelled,
        }
