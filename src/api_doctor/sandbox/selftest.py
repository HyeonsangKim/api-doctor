"""backend 선택과 공통 경계 자가검사 (PRD §7.2 아키텍처 결정).

`select_backend()` 는 경계 자가검사를 **전부 통과한** backend 만 반환한다.
통과하는 backend 가 없으면 `RUNTIME_UNAVAILABLE` 이며, 호스트 실행은 없다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import BoundaryReport, SandboxBackend, SandboxLimits
from .container import ContainerBackend
from .openshell import OpenShellBackend


class RuntimeUnavailable(RuntimeError):
    """검증된 실행 환경이 없습니다."""


@dataclass(frozen=True, slots=True)
class BackendSelection:
    backend: SandboxBackend
    report: BoundaryReport
    rejected: tuple[BoundaryReport, ...]


def select_backend(limits: SandboxLimits | None = None) -> BackendSelection:
    """OpenShell 우선, 컨테이너 대안 순으로 시도한다."""
    limits = limits or SandboxLimits()
    rejected: list[BoundaryReport] = []

    for backend in (OpenShellBackend(), ContainerBackend()):
        try:
            report = backend.boundary_selftest(limits)
        except NotImplementedError as exc:
            rejected.append(
                BoundaryReport(
                    backend_id=backend.backend_id,
                    available=False,
                    unavailable_reason=str(exc),
                )
            )
            continue
        if report.passed:
            return BackendSelection(backend=backend, report=report, rejected=tuple(rejected))
        rejected.append(report)

    raise RuntimeUnavailable(
        "경계 자가검사를 통과한 실행 backend 가 없습니다. 호스트에서 대신 실행하지 않습니다."
    )
