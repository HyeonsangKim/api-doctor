"""API닥터 CLI 진입점.

`--json` 결과는 stdout, 진행 출력은 stderr 로 분리한다 (PRD §5.1.1).
"""

from __future__ import annotations

import typer
from rich.console import Console

from .preflight import run_preflight

app = typer.Typer(
    name="api-doctor",
    help="메인 에이전트와 4개 전문 에이전트의 공공 API 복구",
    no_args_is_help=True,
    add_completion=False,
)

err = Console(stderr=True)


@app.callback()
def _root() -> None:
    """명령 그룹을 고정한다 (typer 가 단일 명령으로 접히지 않게 한다)."""


@app.command()
def preflight() -> None:
    """무료 실행 경로와 격리 경계를 점검한다. 모델을 호출하지 않는다."""
    raise typer.Exit(run_preflight(err))


if __name__ == "__main__":
    app()
