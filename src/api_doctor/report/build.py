"""보고서 생성 (FR-015, AC-15).

`backend_id` 가 openshell 이 아닌 denial 은 OpenShell 증거 절에 넣지 못한다.
문서가 아니라 코드로 막는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.events import Event, EventType, RunStatus
from ..runtime.session import RunSession
from ..sandbox.base import BackendId
from .sanitize import sanitize

_STATUS_TEXT = {
    RunStatus.VERIFIED_REPAIRED: "복구되었고 고정 검증을 통과했습니다",
    RunStatus.VERIFIED_UNCHANGED: "원본이 이미 계약을 만족합니다",
    RunStatus.VERIFICATION_FAILED: "후보가 계약을 만족하지 못했습니다",
    RunStatus.VERIFICATION_INCONCLUSIVE: "판정할 수 없습니다",
    RunStatus.NEEDS_USER_ACTION: "사용자 조치가 필요합니다",
    RunStatus.EXTERNAL_UNAVAILABLE: "외부 자료를 사용할 수 없습니다",
    RunStatus.BUDGET_EXHAUSTED: "예산을 소진했습니다",
    RunStatus.POLICY_BLOCKED: "정책이 차단했습니다",
    RunStatus.CANCELLED: "사용자가 취소했습니다",
    RunStatus.INTERRUPTED: "실행이 중단되었습니다",
}
_MARK = {"pass": "✓", "fail": "✗", "inconclusive": "?",
         "holds": "✓", "violated": "✗", "observed": "·",
         "vacuous": "∅", "unsupported": "?", "error": "!"}


@dataclass(slots=True)
class ReportInputs:
    session: RunSession
    status: RunStatus
    gate: Any | None
    delegations: list[Any]
    model_calls_per_role: dict[str, int]
    scripted: bool = False
    # 복구를 타지 않은 경로(정상 코드·키 없음)에서는 baseline 판정이 곧 결과다.
    baseline_verdict: Any | None = None


def build(inputs: ReportInputs) -> tuple[str, dict[str, Any]]:
    """(report.md, report.json) 을 만든다."""
    payload = _json_report(inputs)
    return _markdown(inputs, payload), payload


# ------------------------------------------------------------------ JSON


def _json_report(inputs: ReportInputs) -> dict[str, Any]:
    session = inputs.session
    usage = session.ledger.usage()
    return {
        "run_id": session.run_id,
        "status": str(inputs.status),
        "dataset_id": session.dataset.dataset_id,
        "source": {
            "kind": session.snapshot.source_kind,
            "snapshot_id": session.snapshot.snapshot_id,
            "collected_at": session.snapshot.collected_at,
            "scope": [session.snapshot.scope_start, session.snapshot.scope_end],
        },
        "model_trace_kind": "scripted" if inputs.scripted else "live_model",
        "hashes": {
            "contract": session.dataset.contract.contract_hash,
            "snapshot": session.snapshot.snapshot_hash,
            "original": session.original.candidate_hash,
            "final_candidate": session.current_hash,
        },
        "verification": _verification(inputs),
        "gate": inputs.gate.to_json() if inputs.gate else None,
        "contributions": _contributions(inputs),
        "probe_results": [r.to_json() for r in session.probe_results.values()],
        "policy_denials": [d.to_json() for d in session.denials],
        "usage": {**usage, "per_role_calls": inputs.model_calls_per_role},
    }


def _verification(inputs: ReportInputs) -> dict[str, Any] | None:
    verdict = inputs.session.final_verdict or inputs.baseline_verdict
    return verdict.to_json() if verdict else None


def _contributions(inputs: ReportInputs) -> list[dict[str, Any]]:
    """역할별 기여 (FR-017). 이름만 등장하는 역할을 구분해 드러낸다."""
    rows: list[dict[str, Any]] = []
    for run in inputs.delegations:
        rows.append({
            "agent_id": run.envelope.agent_id,
            "objective": run.envelope.objective,
            "outcome": str(run.result.outcome),
            "summary": run.result.summary,
            "tools_used": run.tool_calls,
            "turns": run.turns,
            "findings": [f.to_json() for f in run.result.findings],
            "unknowns": list(run.result.unknowns),
            "produced_observation": bool(run.tool_calls),
        })
    return rows


# ------------------------------------------------------------------ Markdown


def _markdown(inputs: ReportInputs, payload: dict[str, Any]) -> str:
    session = inputs.session
    lines: list[str] = []
    add = lines.append

    add(f"# 복구 보고서 — {session.dataset.display_name}")
    add("")
    add(f"> **결과**: {_STATUS_TEXT.get(inputs.status, str(inputs.status))} "
        f"(`{inputs.status}`)")
    add(f"> **작업**: `{session.run_id}`")
    if inputs.scripted:
        add("> **주의**: 이 실행의 모델 응답은 **scripted** 입니다. "
            "실제 모델 trace 가 아닙니다.")
    add("")

    # ---- 자료 출처 ----
    source = payload["source"]
    add("## 자료 출처")
    add("")
    add(f"- 스냅샷 `{source['snapshot_id']}` · 수집 방식 `{source['kind']}`"
        + (f" · 수집 시각 {source['collected_at']}" if source["collected_at"] else ""))
    add(f"- 조회 범위: 레코드 {source['scope'][0]}~{source['scope'][1]} "
        f"(서비스 전체 {session.snapshot.service_total_count}건 중)")
    add(f"- 검증 기준: 수집 시각의 동결 자료")
    add("")

    # ---- 검증 ----
    add("## 고정 검증")
    add("")
    verification = payload["verification"]
    if verification:
        add("| | 검사 | 결과 |")
        add("|---|---|---|")
        for check in verification["checks"]:
            mark = _MARK.get(check["outcome"], "·")
            add(f"| {mark} | `{check['kind']}` | {sanitize(check['summary'])} |")
    else:
        add("종료 검증을 실행하지 못했습니다.")
    add("")

    # ---- 역할별 기여 ----
    add("## 역할별 기여")
    add("")
    if not payload["contributions"]:
        add("전문 역할을 호출하지 않았습니다.")
    else:
        for row in payload["contributions"]:
            evidence = "관측 있음" if row["produced_observation"] else "**관측 없음**"
            add(f"### `{row['agent_id']}` — {row['outcome']} · {evidence}")
            add("")
            add(f"- 요청: {sanitize(row['objective'])}")
            add(f"- 사용 도구: {', '.join(f'`{t}`' for t in row['tools_used']) or '없음'}")
            add(f"- 요약: {sanitize(row['summary'])}")
            for finding in row["findings"]:
                # 감사 형식이 아닌 자유 관측은 risk_id 가 비어 있다.
                # 빈 backtick 과 의미 없는 inconclusive 를 늘어놓으면
                # 보고서가 읽히지 않는다.
                text = sanitize(finding["hypothesis"])[:140]
                if finding["risk_id"]:
                    add(f"- `{finding['risk_id']}` → **{finding['conclusion']}**"
                        + (f" ({text})" if text else ""))
                elif text:
                    add(f"- 관측: {text}")
            for unknown in row["unknowns"]:
                add(f"- 미해결: {sanitize(unknown)}")
            add("")

    # ---- probe ----
    if payload["probe_results"]:
        add("## 실행된 probe")
        add("")
        add("| | probe | 불변식 | 관측 |")
        add("|---|---|---|---|")
        for result in payload["probe_results"]:
            mark = _MARK.get(result["outcome"], "·")
            add(f"| {mark} | `{result['probe_id']}` | `{result['invariant']}` | "
                f"{sanitize(result['summary'])} |")
        add("")

    # ---- 격리 차단 ----
    add("## 격리 차단 기록")
    add("")
    add(_denials_section(payload["policy_denials"], session))
    add("")

    # ---- 사용량 ----
    usage = payload["usage"]
    add("## 사용량")
    add("")
    add(f"- 모델 호출 {usage['model_calls']}회 · 토큰 {usage['tokens']:,} "
        f"({usage['tokens_kind']})")
    add(f"- 격리 실행 {usage['sandbox_runs']}회 · 데이터 호출 "
        f"{usage['broker_calls']}회 · 패치 {usage['patches']}개")
    add("")
    add("| 역할 | 모델 호출 |")
    add("|---|---:|")
    for role, count in usage["per_role_calls"].items():
        if count:
            add(f"| `{role}` | {count} |")
    add("")
    total = sum(usage["per_role_calls"].values())
    if total == usage["model_calls"]:
        add(f"역할별 합계 {total} = gateway 원장 {usage['model_calls']}. 일치합니다.")
    else:
        add(f"⚠ 역할별 합계 {total} ≠ gateway 원장 {usage['model_calls']}. "
            "예산 판단은 원장을 따릅니다.")
    add("")

    return "\n".join(lines)


def _denials_section(denials: list[dict[str, Any]], session: RunSession) -> str:
    """AC-15: 컨테이너 결과를 OpenShell 증거로 표시하지 않는다.

    `backend_id` 로 분리하며, 이 함수 밖에서 라벨을 바꿀 수 없다.
    """
    if not denials:
        return ("차단 기록이 없습니다. 차단이 일어나지 않았거나 수집하지 못했습니다. "
                "**차단 성공을 주장하지 않습니다.**")

    by_backend: dict[str, list[dict[str, Any]]] = {}
    for denial in denials:
        by_backend.setdefault(str(denial["backend_id"]), []).append(denial)

    chunks: list[str] = []
    for backend, rows in sorted(by_backend.items()):
        label = (
            "OpenShell 정책 차단"
            if backend == str(BackendId.OPENSHELL)
            else f"컨테이너 대안 backend 차단 (`{backend}`)"
        )
        chunks.append(f"### {label} — {len(rows)}건")
        chunks.append("")
        chunks.append("| 시각 | 목적지 | binary | 사유 |")
        chunks.append("|---|---|---|---|")
        for row in rows[:20]:
            chunks.append(
                f"| {row['occurred_at']} | {sanitize(str(row['destination'] or '-'))[:60]} "
                f"| `{row['binary'] or '-'}` | {sanitize(row['reason'])[:90]} |"
            )
        if backend != str(BackendId.OPENSHELL):
            chunks.append("")
            chunks.append(
                "> 이 기록은 컨테이너 대안 backend 의 것입니다. "
                "**OpenShell 차단 증거로 사용하지 않습니다.**"
            )
        chunks.append("")
    return "\n".join(chunks)


def write(inputs: ReportInputs) -> tuple[Path, Path]:
    markdown, payload = build(inputs)
    paths = inputs.session.paths
    md_path = paths.run_dir / "report.md"
    json_path = paths.run_dir / "report.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.chmod(0o600)
    json_path.chmod(0o600)
    return md_path, json_path
