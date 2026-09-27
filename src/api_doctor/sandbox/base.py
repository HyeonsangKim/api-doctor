"""Sandbox backend 프로토콜.

Zone S 경계를 정의한다. 이 모듈에는 `HostBackend`가 없고 앞으로도 없다.
어떤 backend도 `boundary_selftest()`를 전부 통과하기 전에는 후보 코드를 실행하지 않는다 (AC-10).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class BackendId(StrEnum):
    """실행 backend 식별자.

    보고서 생성기는 이 값으로 증거를 분리한다. 컨테이너 결과를 OpenShell 증거로
    표시하는 것은 PRD AC-15 위반이며 `report/build.py`가 코드로 막는다.
    """

    OPENSHELL = "openshell"
    CONTAINER = "container"


class BoundaryCase(StrEnum):
    """모든 backend가 동일하게 통과해야 하는 경계 자가검사 항목."""

    NETWORK_EGRESS = "network_egress"
    WRITE_OUTSIDE_WORKDIR = "write_outside_workdir"
    READ_HOST_SECRET = "read_host_secret"
    PROCESS_LIMIT = "process_limit"
    PRIVILEGE_ESCALATION = "privilege_escalation"


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    """경계 검사 1건의 결과."""

    case: BoundaryCase
    blocked: bool
    detail: str


@dataclass(frozen=True, slots=True)
class BoundaryReport:
    """backend 1개의 경계 자가검사 전체 결과."""

    backend_id: BackendId
    available: bool
    results: tuple[BoundaryResult, ...] = ()
    unavailable_reason: str | None = None

    @property
    def passed(self) -> bool:
        """모든 경계 항목이 차단됐고 backend가 가용할 때만 참."""
        if not self.available or not self.results:
            return False
        covered = {r.case for r in self.results}
        if covered != set(BoundaryCase):
            return False
        return all(r.blocked for r in self.results)

    @property
    def failures(self) -> tuple[BoundaryResult, ...]:
        return tuple(r for r in self.results if not r.blocked)


@dataclass(frozen=True, slots=True)
class Denial:
    """격리 정책이 차단한 시도 1건.

    `backend_id`는 필수다. 이 필드 없이는 denial을 기록하지 않는다 (AC-15).
    """

    backend_id: BackendId
    destination: str | None
    binary: str | None
    reason: str
    occurred_at: str
    # policy: 격리·접근 정책 위반 (복구 대상 아님)
    # unsupported_request: 동결 자료에 없는 요청 (코드 결함이며 복구 대상)
    kind: str = "policy"

    @property
    def is_policy_violation(self) -> bool:
        return self.kind == "policy"

    def to_json(self) -> dict[str, Any]:
        return {
            "backend_id": str(self.backend_id),
            "destination": self.destination,
            "binary": self.binary,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """PRD §4.1 격리 실행 상한."""

    wall_seconds: int = 15
    cpus: float = 1.0
    memory_mib: int = 512
    pids: int = 32
    tmpfs_mib: int = 16
    max_stdout_bytes: int = 1 << 20


@dataclass(slots=True)
class SandboxOutcome:
    """후보 1회 실행의 결과."""

    backend_id: BackendId
    exit_code: int | None
    records: list[dict[str, Any]] | None
    error: str | None
    duration_ms: int
    requests_made: int
    denials: list[Denial] = field(default_factory=list)
    stdout_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.records is not None


@runtime_checkable
class SandboxBackend(Protocol):
    """Zone S backend가 만족해야 하는 계약."""

    backend_id: BackendId

    def boundary_selftest(self, limits: SandboxLimits) -> BoundaryReport:
        """경계가 실제로 작동하는지 확인한다. 후보 실행 전 매번 호출된다."""
        ...

    def run_candidate(
        self,
        candidate_path: Path,
        query: dict[str, Any],
        respond: "BrokerRespond",
        limits: SandboxLimits,
    ) -> SandboxOutcome:
        """후보의 `fetch_records(http, query)`를 격리 실행한다.

        `respond`는 Zone T의 broker 콜백이다. 후보는 소켓을 얻지 못하고,
        모든 요청은 이 콜백을 통해서만 동결 스냅샷으로 해소된다.
        """
        ...


class BrokerRespond(Protocol):
    """후보의 http 요청 1건을 동결 데이터로 해소하는 Zone T 콜백."""

    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """{"status": int, "headers": dict, "body": str} 또는 거절 응답을 반환."""
        ...


def utc_now() -> str:
    """denial·event 타임스탬프용 UTC ISO-8601."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
