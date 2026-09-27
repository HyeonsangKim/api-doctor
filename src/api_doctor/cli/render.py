"""터미널 표시 (PRD §4.5 보고서 정리)."""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console
from rich.table import Table

from ..runtime.events import RunStatus
from ..verify.verifier import Outcome, Verdict

# CLI escape sequence 와 제어문자를 제거한다. 외부 자료가 터미널을 조작하지 못하게 한다.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]")

_MARK = {Outcome.PASS: "[green]✓[/]", Outcome.FAIL: "[red]✗[/]",
         Outcome.INCONCLUSIVE: "[yellow]?[/]"}

_STATUS_STYLE = {
    RunStatus.VERIFIED_REPAIRED: "bold green",
    RunStatus.VERIFIED_UNCHANGED: "bold green",
    RunStatus.VERIFICATION_FAILED: "bold red",
    RunStatus.VERIFICATION_INCONCLUSIVE: "bold yellow",
    RunStatus.POLICY_BLOCKED: "bold yellow",
    RunStatus.BUDGET_EXHAUSTED: "bold yellow",
    RunStatus.CANCELLED: "bold yellow",
}


def sanitize(text: str) -> str:
    return _CONTROL.sub("", text)


def status_text(status: RunStatus) -> str:
    return f"[{_STATUS_STYLE.get(status, 'bold')}]{status}[/]"


def print_verdict(console: Console, verdict: Verdict) -> None:
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2, 0, 0))
    table.add_column("", width=1)
    table.add_column("검사")
    table.add_column("결과", overflow="fold")
    for result in verdict.results:
        table.add_row(_MARK[result.outcome], str(result.kind), sanitize(result.summary))
    console.print(table)


def print_denials(console: Console, denials: list[Any]) -> None:
    if not denials:
        return
    console.print(f"\n[yellow]격리 차단 기록 {len(denials)}건[/]")
    for denial in denials:
        dest = denial.destination or "-"
        console.print(
            f"  [dim]{denial.occurred_at}[/] [{denial.backend_id}] "
            f"{sanitize(dest)[:60]} · {sanitize(denial.reason)[:80]}"
        )
