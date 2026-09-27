"""OpenShell backend (FR-021, PRD §5.3.5).

우선 backend 이지만 Linux 대상이다. 가용하지 않으면 **가용하지 않다고 기록**하고
컨테이너 대안으로 넘어간다. 호스트 실행으로 대체하는 경로는 존재하지 않는다 (AC-10).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import (
    BackendId,
    BoundaryReport,
    BrokerRespond,
    SandboxLimits,
    SandboxOutcome,
)


class OpenShellBackend:
    """OpenShell 실행기. 현재는 가용성 판정과 정책 hash 고정까지 구현한다."""

    backend_id = BackendId.OPENSHELL

    def __init__(self, policy_path: Path | None = None) -> None:
        self.policy_path = policy_path

    def availability(self) -> tuple[bool, str | None]:
        """(가용 여부, 불가 사유)."""
        system = platform.system()
        if system != "Linux":
            return False, f"OpenShell 은 Linux 대상입니다. 현재 플랫폼: {system}"
        binary = shutil.which("openshell")
        if binary is None:
            return False, "openshell 실행 파일을 찾을 수 없습니다."
        try:
            proc = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"openshell 호출 실패: {type(exc).__name__}"
        if proc.returncode != 0:
            return False, f"openshell --version 실패 rc={proc.returncode}"
        return True, None

    def boundary_selftest(self, limits: SandboxLimits) -> BoundaryReport:
        available, reason = self.availability()
        if not available:
            return BoundaryReport(
                backend_id=self.backend_id,
                available=False,
                unavailable_reason=reason,
            )
        raise NotImplementedError(
            "OpenShell 경계 자가검사는 지원 환경(Linux)에서 구현합니다. "
            "컨테이너 결과를 OpenShell 증거로 대체 표기하지 않습니다."
        )

    def run_candidate(
        self,
        candidate_path: Path,
        query: dict[str, Any],
        respond: BrokerRespond,
        limits: SandboxLimits,
    ) -> SandboxOutcome:
        raise NotImplementedError(
            "OpenShell 후보 실행은 지원 환경에서 구현합니다."
        )
