"""사전 점검 — 무료 실행 경로가 실재하는지 확인한다 (PRD Phase 0).

모델 없이 돌며, 확인되지 않은 것은 "확인됨"으로 표시하지 않는다.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum

from rich.console import Console
from rich.table import Table

from ..sandbox.base import SandboxLimits
from ..sandbox.container import ContainerBackend
from ..sandbox.openshell import OpenShellBackend


class Verdict(StrEnum):
    OK = "확인됨"
    BLOCKED = "차단"
    MISSING = "미확인"
    FAILED = "실패"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    verdict: Verdict
    detail: str


_STYLE = {
    Verdict.OK: "green",
    Verdict.BLOCKED: "yellow",
    Verdict.MISSING: "yellow",
    Verdict.FAILED: "red",
}


def check_python() -> Check:
    major, minor = sys.version_info[:2]
    supported = (major, minor) == (3, 12)
    return Check(
        "Python 런타임",
        Verdict.OK if supported else Verdict.FAILED,
        f"{platform.python_version()} on {platform.system()} {platform.machine()}",
    )


def check_model_key() -> Check:
    """키의 **존재 여부만** 본다. 실제 호출·한도 확인은 별도 단계다."""
    if os.environ.get("NVIDIA_API_KEY"):
        return Check(
            "NVIDIA_API_KEY",
            Verdict.OK,
            "설정됨 (실제 호출·무료 한도는 미확인)",
        )
    return Check(
        "NVIDIA_API_KEY",
        Verdict.MISSING,
        "미설정 — M2(에이전트) 차단. M0·M1 은 진행 가능",
    )


def check_sandbox(limits: SandboxLimits) -> list[Check]:
    checks: list[Check] = []

    openshell = OpenShellBackend()
    available, reason = openshell.availability()
    checks.append(
        Check(
            "OpenShell backend",
            Verdict.OK if available else Verdict.MISSING,
            reason or "가용",
        )
    )

    report = ContainerBackend().boundary_selftest(limits)
    if not report.available:
        checks.append(
            Check("Container backend", Verdict.FAILED, report.unavailable_reason or "가용하지 않음")
        )
        return checks

    checks.append(
        Check(
            "Container backend",
            Verdict.OK if report.passed else Verdict.FAILED,
            f"경계 {sum(r.blocked for r in report.results)}/{len(report.results)} 차단",
        )
    )
    for result in report.results:
        checks.append(
            Check(
                f"  경계 · {result.case}",
                Verdict.BLOCKED if result.blocked else Verdict.FAILED,
                result.detail[:100],
            )
        )
    return checks


def run_preflight(console: Console | None = None) -> int:
    """0 = 후보 실행 가능, 1 = 실행 환경 없음."""
    console = console or Console(stderr=True)
    limits = SandboxLimits()

    checks = [check_python(), check_model_key(), *check_sandbox(limits)]

    table = Table(title="API닥터 사전 점검", title_style="bold", show_lines=False)
    table.add_column("항목", style="bold")
    table.add_column("판정")
    table.add_column("내용", overflow="fold")
    for check in checks:
        table.add_row(
            check.name,
            f"[{_STYLE[check.verdict]}]{check.verdict}[/]",
            check.detail,
        )
    console.print(table)

    runnable = any(
        c.name == "Container backend" and c.verdict is Verdict.OK for c in checks
    ) or any(c.name == "OpenShell backend" and c.verdict is Verdict.OK for c in checks)

    if runnable:
        console.print("[green]격리 실행 경로가 확인되었습니다.[/]")
        return 0
    console.print(
        "[red]검증된 실행 backend 가 없습니다. 호스트에서 대신 실행하지 않습니다.[/]"
    )
    return 1
