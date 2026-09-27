"""역할별 사용량 프로파일 (FR-018, AC-16).

**출처 표기가 중요하다.** 여기의 산출물은 NVIDIA NeMo Agent Toolkit 이 아니라
**우리 gateway 원장**에서 만든 것이다. PRD R-13 이 대비해 둔 경로다:

> NAT 가 Deep Agents 하위 에이전트 호출을 역할별로 구분하지 못하면 전체 합계만
> 보고하고 역할별 수치는 gateway 원장으로 한다.

원장은 모든 모델 호출이 반드시 지나는 단일 통로이므로 (FR-003), 역할별 구분이
구조적으로 보장된다. NAT 프로파일러를 실제로 돌렸다면 `source` 를 바꾸고
`reconcile()` 로 두 수치를 대조한다.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model.gateway import ModelCall
from ..runtime.budget import AGENT_IDS

SOURCE_LEDGER = "gateway_ledger"
SOURCE_NAT = "nemo_agent_toolkit"

# 역할별 띠 색. 보고서·Gantt 에서 같은 색을 쓴다.
_COLORS = {
    "main": "#3b5bdb",
    "spec_researcher": "#2b8a3e",
    "runtime_diagnostician": "#e8590c",
    "repair_engineer": "#9c36b5",
    "data_auditor": "#c92a2a",
}
_TOOL_COLOR = "#868e96"


@dataclass(frozen=True, slots=True)
class Span:
    """Gantt 의 한 칸."""

    agent_id: str
    kind: str          # model | tool
    label: str
    started_ms: int
    ended_ms: int
    detail: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.ended_ms - self.started_ms)


def collect_spans(
    model_calls: list[ModelCall], tool_log: list[dict[str, Any]]
) -> list[Span]:
    spans = [
        Span(
            agent_id=call.agent_id, kind="model",
            label=f"model:{call.agent_id}",
            started_ms=call.started_ms, ended_ms=call.ended_ms,
            detail=f"{call.total_tokens or 0} tokens ({call.tokens_kind})",
        )
        for call in model_calls
    ]
    spans += [
        Span(
            agent_id=str(entry.get("agent_id", "?")), kind="tool",
            label=str(entry.get("tool", "?")),
            started_ms=int(entry.get("started_ms", 0)),
            ended_ms=int(entry.get("ended_ms", 0)),
            detail="allowed" if entry.get("allowed") else str(entry.get("reason")),
        )
        for entry in tool_log
    ]
    return sorted(spans, key=lambda s: (s.started_ms, s.agent_id))


def per_role(model_calls: list[ModelCall]) -> dict[str, dict[str, Any]]:
    """역할별 호출 수·토큰·지연 (FR-018 의 측정 대상)."""
    rows: dict[str, dict[str, Any]] = {}
    for call in model_calls:
        row = rows.setdefault(
            call.agent_id,
            {"calls": 0, "tokens": 0, "latency_ms": 0,
             "estimated_calls": 0, "errors": 0},
        )
        row["calls"] += 1
        row["tokens"] += call.total_tokens or 0
        row["latency_ms"] += call.latency_ms
        if call.tokens_kind == "estimated":
            row["estimated_calls"] += 1
        if call.error:
            row["errors"] += 1
    for row in rows.values():
        row["avg_latency_ms"] = round(row["latency_ms"] / max(1, row["calls"]), 1)
    return rows


# ------------------------------------------------------------------ 산출물


def write_csv(path: Path, spans: list[Span], source: str) -> Path:
    """NAT 의 `standardized_data_all.csv` 에 대응하는 구간 표."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["source", "agent_id", "kind", "label",
             "started_ms", "ended_ms", "duration_ms", "detail"]
        )
        for span in spans:
            writer.writerow([
                source, span.agent_id, span.kind, span.label,
                span.started_ms, span.ended_ms, span.duration_ms, span.detail,
            ])
    path.chmod(0o600)
    return path


def write_report(
    path: Path,
    model_calls: list[ModelCall],
    spans: list[Span],
    ledger_usage: dict[str, Any],
    source: str,
) -> Path:
    """NAT 의 `workflow_profiling_report.txt` 에 대응하는 요약."""
    rows = per_role(model_calls)
    lines: list[str] = []
    add = lines.append

    add("API닥터 — 역할별 사용량 프로파일")
    add("=" * 58)
    add(f"출처: {source}")
    if source == SOURCE_LEDGER:
        add("  NVIDIA NeMo Agent Toolkit 이 아니라 공통 예산 gateway 원장에서")
        add("  산출했다. 원장은 모든 모델 호출이 반드시 지나는 단일 통로이므로")
        add("  역할별 구분이 구조적으로 보장된다 (FR-003).")
    add("")

    add(f"{'역할':24}{'호출':>6}{'토큰':>10}{'평균지연ms':>12}{'추정':>6}")
    add("-" * 58)
    for agent_id in AGENT_IDS:
        row = rows.get(agent_id)
        if not row:
            continue
        add(f"{agent_id:24}{row['calls']:>6}{row['tokens']:>10}"
            f"{row['avg_latency_ms']:>12}{row['estimated_calls']:>6}")
    add("-" * 58)
    total_calls = sum(r["calls"] for r in rows.values())
    total_tokens = sum(r["tokens"] for r in rows.values())
    add(f"{'합계':24}{total_calls:>6}{total_tokens:>10}")
    add("")

    # AC-16: 역할별 합계가 원장과 일치해야 한다.
    ledger_calls = int(ledger_usage.get("model_calls", 0))
    add("원장 대조 (AC-16)")
    add(f"  역할별 합계 {total_calls} · gateway 원장 {ledger_calls}")
    if total_calls == ledger_calls:
        add("  일치합니다.")
    else:
        add(f"  차이 {abs(total_calls - ledger_calls)}건. 예산 판단은 원장을 따릅니다.")
    add("")

    tool_spans = [s for s in spans if s.kind == "tool"]
    add(f"도구 구간 {len(tool_spans)}건")
    by_tool: dict[str, int] = {}
    for span in tool_spans:
        by_tool[span.label] = by_tool.get(span.label, 0) + 1
    for label, count in sorted(by_tool.items(), key=lambda x: -x[1]):
        add(f"  {label:22} {count}회")
    add("")
    if spans:
        add(f"전체 구간 {spans[0].started_ms}ms ~ {max(s.ended_ms for s in spans)}ms")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def write_gantt(path: Path, spans: list[Span], source: str) -> Path:
    """역할별 Gantt (FR-018).

    외부 의존성 없이 SVG 를 직접 그린다. PRD 는 NAT 의 산출물 이름을
    `gantt_chart.png` 로 적었으나, 우리 것은 원장에서 만든 SVG 다.
    """
    if not spans:
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="60">'
            '<text x="12" y="34" font-size="14">구간 기록이 없습니다</text></svg>\n',
            encoding="utf-8",
        )
        return path

    lanes = [a for a in AGENT_IDS if any(s.agent_id == a for s in spans)]
    total_ms = max(s.ended_ms for s in spans) or 1

    left, row_h, top = 190, 34, 56
    width, plot_w = 1040, 1040 - 190 - 24
    height = top + row_h * len(lanes) + 46

    def x_of(ms: int) -> float:
        return left + plot_w * (ms / total_ms)

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" font-family="Apple SD Gothic Neo, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="20" y="28" font-size="15" font-weight="600">'
        f'역할별 실행 구간 · 출처 {_escape(source)}</text>',
    ]

    # 눈금
    for step in range(0, 6):
        ms = int(total_ms * step / 5)
        x = x_of(ms)
        out.append(f'<line x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" '
                   f'y2="{height - 30}" stroke="#e9ecef" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{height - 14}" font-size="11" '
                   f'fill="#868e96" text-anchor="middle">{ms}ms</text>')

    for index, agent_id in enumerate(lanes):
        y = top + row_h * index
        out.append(
            f'<text x="20" y="{y + 15}" font-size="12">{_escape(agent_id)}</text>'
        )
        out.append(f'<line x1="{left}" y1="{y + 20}" x2="{width - 24}" '
                   f'y2="{y + 20}" stroke="#f1f3f5" stroke-width="1"/>')
        for span in spans:
            if span.agent_id != agent_id:
                continue
            x1, x2 = x_of(span.started_ms), x_of(span.ended_ms)
            bar_w = max(2.0, x2 - x1)
            is_model = span.kind == "model"
            color = _COLORS.get(agent_id, "#495057") if is_model else _TOOL_COLOR
            y_off = 0 if is_model else 11
            bar_h = 10 if is_model else 7
            opacity = "" if is_model else ' opacity="0.75"'
            title = _escape(f"{span.label} · {span.duration_ms}ms · {span.detail}")
            out.append(
                f'<rect x="{x1:.1f}" y="{y + y_off}" width="{bar_w:.1f}" '
                f'height="{bar_h}" rx="2" fill="{color}"{opacity}>'
                f'<title>{title}</title></rect>'
            )

    out.append(f'<rect x="{left}" y="{height - 40}" width="10" height="9" '
               f'rx="2" fill="#3b5bdb"/>')
    out.append(f'<text x="{left + 16}" y="{height - 32}" font-size="11" '
               f'fill="#495057">모델 호출</text>')
    out.append(f'<rect x="{left + 80}" y="{height - 39}" width="10" height="7" '
               f'rx="2" fill="{_TOOL_COLOR}" opacity="0.75"/>')
    out.append(f'<text x="{left + 96}" y="{height - 32}" font-size="11" '
               f'fill="#495057">도구 호출</text>')
    out.append("</svg>")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _escape(text: str) -> str:
    """SVG 텍스트 이스케이프. 도구 결과 문자열이 마크업을 깨지 못하게 한다."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def write_all(
    run_dir: Path,
    model_calls: list[ModelCall],
    tool_log: list[dict[str, Any]],
    ledger_usage: dict[str, Any],
    source: str = SOURCE_LEDGER,
) -> dict[str, Path]:
    """세 산출물을 한 번에 만든다 (FR-018)."""
    directory = run_dir / "profiling"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    spans = collect_spans(model_calls, tool_log)
    return {
        "csv": write_csv(directory / "standardized_data_all.csv", spans, source),
        "report": write_report(
            directory / "workflow_profiling_report.txt",
            model_calls, spans, ledger_usage, source,
        ),
        "gantt": write_gantt(directory / "gantt_chart.svg", spans, source),
    }


def reconcile(
    model_calls: list[ModelCall], profiler_counts: dict[str, int]
) -> dict[str, Any]:
    """NAT 프로파일러 수치와 원장을 대조한다 (AC-16).

    다르면 차이를 보고하고 **예산 판단은 원장을 따른다** (PRD 명시).
    """
    ledger = {k: v["calls"] for k, v in per_role(model_calls).items()}
    roles = sorted(set(ledger) | set(profiler_counts))
    differences = {
        role: {"ledger": ledger.get(role, 0), "profiler": profiler_counts.get(role, 0)}
        for role in roles
        if ledger.get(role, 0) != profiler_counts.get(role, 0)
    }
    return {
        "matches": not differences,
        "ledger_total": sum(ledger.values()),
        "profiler_total": sum(profiler_counts.values()),
        "differences": differences,
        "authority": "gateway_ledger",
    }
