"""replay — 저장 기록만 표시한다 (FR-015, AC-12).

이 모듈은 `model` · `sandbox` · `data` · `agents` 를 import 하지 않는다.
"외부 호출 0회"를 런타임 플래그가 아니라 **의존성 부재**로 보장한다.
`tests/test_import_boundaries.py` 가 이를 검사한다.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console

from ..runtime.events import Event, EventStore, verify_sequence
from ..runtime.store import Manifest, RunPaths

_ICON: dict[str, str] = {
    "run_started": "▶", "stage_changed": "→", "baseline_result": "◆",
    "sandbox_run": "⚙", "check_result": "·", "policy_denied": "⨯",
    "run_finished": "■", "model_call": "✦", "tool_call": "⌁",
    "delegation_requested": "⇢", "delegation_finished": "⇠",
}
_STYLE: dict[str, str] = {
    "policy_denied": "yellow", "run_finished": "bold",
    "baseline_result": "bold cyan", "run_started": "bold",
}


def format_event(event: Event) -> str:
    icon = _ICON.get(str(event.type), "·")
    style = _STYLE.get(str(event.type))
    text = f"{icon} [dim]{event.time}[/] {event.summary}"
    return f"[{style}]{text}[/]" if style else text


def replay_run(
    paths: RunPaths, console: Console, *, delay: float = 0.0
) -> dict[str, Any]:
    """기록을 원래 순서대로 출력한다. 모델·API·코드 실행은 0회다."""
    events = EventStore.read(paths.run_dir)
    verify_sequence(events)
    manifest = Manifest.load(paths)

    console.print(
        f"[bold]replay[/] {manifest.run_id} "
        f"[dim]원래 시각 {manifest.created_at} · 원래 판정 {manifest.status}[/]"
    )
    console.print("[dim]" + "─" * 64 + "[/]")
    for event in events:
        console.print(format_event(event))
        if delay:
            time.sleep(delay)
    console.print("[dim]" + "─" * 64 + "[/]")
    console.print(
        f"[dim]사건 {len(events)}건 · 이번 표시에서 모델 0회 · 외부 API 0회 · 코드 실행 0회[/]"
    )

    return {
        "run_id": manifest.run_id,
        "display_mode": "replay",
        "original_status": manifest.status,
        "original_created_at": manifest.created_at,
        "event_count": len(events),
        "external_calls": 0,
    }
