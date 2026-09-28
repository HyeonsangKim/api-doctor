"""API닥터 CLI 진입점 (FR-015).

`--json` 결과는 stdout, 진행 출력은 stderr 로 분리한다 (PRD §5.1.1).
종료 코드: 0=성공, 2=입력/권한/자료, 3=복구 실패·보류, 4=예산·정책, 5=BUSY·내부, 130=취소.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from ..registry.loader import RegistryInvalid, load_registry
from ..runtime.baseline import InputError
from ..runtime.recover import recover
from ..runtime.events import Event, EventStore, RunStatus, verify_sequence
from ..runtime.store import Manifest, Resolution, RunStore, StoreError
from .preflight import run_preflight
from .render import print_denials, print_verdict, sanitize, status_text
from .replay import format_event, replay_run

app = typer.Typer(
    name="api-doctor",
    help="메인 에이전트와 4개 전문 에이전트의 공공 API 복구",
    no_args_is_help=True,
    add_completion=False,
)

err = Console(stderr=True)
out = Console()


@app.callback()
def _root() -> None:
    """명령 그룹을 고정한다."""


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        out.print_json(json.dumps(payload, ensure_ascii=False))


def _fail(code: str, message: str, exit_code: int, as_json: bool) -> None:
    err.print(f"[red]{code}[/] {sanitize(message)}")
    _emit({"error": {"code": code, "message": message, "retryable": False},
           "run_id": None}, as_json)
    raise typer.Exit(exit_code)


def _resolve_or_fail(run_id: str, as_json: bool):
    store = RunStore()
    resolution, paths = store.resolve(run_id)
    if resolution is not Resolution.OK or paths is None:
        _fail(str(resolution), f"{run_id}", 2, as_json)
    return store, paths


# --------------------------------------------------------------------- 명령


@app.command()
def preflight() -> None:
    """무료 실행 경로와 격리 경계를 점검한다. 모델을 호출하지 않는다."""
    raise typer.Exit(run_preflight(err))


@app.command()
def datasets(
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """등록된 데이터셋과 계약·스킬 버전을 보여준다."""
    try:
        registry = load_registry()
    except RegistryInvalid as exc:
        _fail("REGISTRY_INVALID", str(exc), 2, as_json)
        return

    rows = []
    for dataset in registry.values():
        snapshots = sorted(p.stem for p in (dataset.root / "snapshots").glob("*.json"))
        rows.append({
            "dataset_id": dataset.dataset_id,
            "display_name": dataset.display_name,
            "contract": f"{dataset.contract.contract_id}@{dataset.contract.version}",
            "live_ready": dataset.live_ready,
            "live_blockers": list(dataset.live_blockers),
            "snapshots": snapshots,
            "source_kind": dataset.provenance.get("source_kind"),
            "audit_risks": [a.risk_id for a in dataset.contract.audit_requirements],
            "probes": [p.probe_id for p in dataset.probes.probes],
            "skill": dataset.skill,
        })

    if not as_json:
        for row in rows:
            err.print(f"[bold]{row['dataset_id']}[/]  {row['display_name']}")
            err.print(f"  계약      {row['contract']}")
            err.print(f"  출처      {row['source_kind']}")
            err.print(
                f"  live      {'준비됨' if row['live_ready'] else '미준비'}"
                + ("" if row["live_ready"] else f" — {'; '.join(row['live_blockers'])}")
            )
            err.print(f"  스냅샷    {', '.join(row['snapshots']) or '없음'}")
            err.print(f"  감사 영역 {', '.join(row['audit_risks'])}")
            err.print(f"  probe     {', '.join(row['probes'])}")
    _emit({"datasets": rows}, as_json)


@app.command()
def run(
    dataset: Annotated[str, typer.Option("--dataset", "-d", help="등록된 dataset_id")],
    code: Annotated[Path, typer.Option("--code", "-c", help="연결 파일 경로")],
    snapshot: Annotated[str | None, typer.Option("--snapshot", help="동결 스냅샷 ID")] = None,
    source: Annotated[str, typer.Option("--source", help="fixture | live")] = "fixture",
    harness: Annotated[str, typer.Option(
        "--harness", help="deepagents | builtin | single")] = "deepagents",
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """깨진 연결을 복구한다."""
    def on_event(event: Event) -> None:
        if not as_json:
            err.print(format_event(event))

    try:
        result = recover(
            dataset_id=dataset, code_path=code, snapshot_id=snapshot,
            source=source, harness=harness, on_event=on_event,
        )
    except InputError as exc:
        _fail(exc.code, str(exc), 2, as_json)
        return
    except StoreError as exc:
        _fail(exc.code, str(exc), 5, as_json)
        return

    baseline = result.baseline
    session = baseline.session

    if not as_json:
        err.print()
        verdict = (session.final_verdict if session and session.final_verdict
                   else baseline.verdict)
        if verdict is not None:
            print_verdict(err, verdict)
        if result.orchestration:
            err.print()
            for run in result.orchestration.delegations:
                tools = ", ".join(run.tool_calls) or "도구 없음"
                err.print(f"  [bold]{run.envelope.agent_id}[/] "
                          f"[dim]{run.result.outcome} · {tools}[/]")
                err.print(f"    {sanitize(run.result.summary)[:90]}")
        print_denials(err, result.baseline.denials + (session.denials if session else []))
        err.print(f"\n{status_text(result.status)}  [dim]{result.baseline.run_id}[/]")
        if not result.model_available:
            err.print(f"[yellow]복구를 시도하지 않았습니다[/] {result.model_error}")
        if result.report_paths:
            err.print(f"[dim]보고서: {result.report_paths[0]}[/]")

    usage = session.ledger.usage() if session else {}
    _emit({
        "run_id": result.baseline.run_id,
        "status": str(result.status),
        "source": source,
        "candidate_hash": session.current_hash if session else None,
        "verification": {
            "contract_version": baseline.manifest.hashes.get("contract"),
            "gate": "passed" if result.status.is_success else "failed",
            "checks": (
                (session.final_verdict or baseline.verdict).to_json()["checks"]
                if session and (session.final_verdict or baseline.verdict) else []
            ),
        },
        "contributions": [
            {"agent_id": r.envelope.agent_id, "outcome": str(r.result.outcome),
             "tools_used": r.tool_calls}
            for r in (result.orchestration.delegations if result.orchestration else [])
        ],
        "usage": usage,
        "artifacts": result.artifacts,
        "error": result.model_error or baseline.error,
    }, as_json)
    raise typer.Exit(result.status.exit_code)


@app.command(name="eval")
def evaluate(
    split: Annotated[str, typer.Option("--split", help="dev | locked | all")] = "all",
    recovery: Annotated[bool, typer.Option(
        "--recovery", help="실제 모델로 복구율을 측정한다 (크레딧 소모)")] = False,
    harness: Annotated[str, typer.Option(
        "--harness", help="deepagents | builtin | single")] = "deepagents",
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """평가 세트로 검출 정확도 또는 복구율을 측정한다."""
    from ..evaluation.runner import run_detection, run_recovery

    root = Path(__file__).resolve().parents[3] / "eval"
    if not (root / "cases.json").is_file():
        _fail("NOT_FOUND", f"평가 세트를 찾을 수 없습니다: {root}", 2, as_json)
        return

    def on_case(result: Any) -> None:
        if not as_json:
            mark = "[green]✓[/]" if result.detected else "[red]✗[/]"
            baseline = {True: "통과", False: "실패", None: "미실행"}[
                result.baseline_passed
            ]
            err.print(
                f"  {mark} {result.name:34} [dim]{result.kind:10}"
                f"{result.split:7}baseline={baseline:5}"
                + (f" 차단{result.policy_denials}" if result.policy_denials else "")
                + "[/]"
            )

    store_root = RunStore().root.parent / "eval-runs"

    if recovery:
        _run_recovery_eval(root, store_root, split, harness, as_json)
        return

    if not as_json:
        err.print(f"[bold]평가 세트[/] split={split}\n")
    report = run_detection(
        cases_root=root, store_root=store_root, split=split, on_case=on_case
    )

    payload = report.to_json()
    if not as_json:
        summary = payload["summary"]
        err.print()
        err.print(f"[bold]검출 {summary['detection_correct']}/{summary['total']}[/]")
        fp, fs = summary["false_positives"], summary["false_successes"]
        err.print(
            f"  정상 훼손 [{'green' if fp == 0 else 'red'}]{fp}건[/]"
            f" · 거짓 성공 [{'green' if fs == 0 else 'red'}]{fs}건[/]"
        )
        for kind, row in sorted(summary["by_kind"].items()):
            style = "green" if row["ok"] == row["total"] else "red"
            err.print(f"  {kind:12} [{style}]{row['ok']}/{row['total']}[/]")
        err.print(f"\n[dim]{summary['recovery_skipped_reason']}[/]")
    _emit(payload, as_json)
    raise typer.Exit(0 if report.detected == len(report.results) else 3)


def _run_recovery_eval(
    root: Path, store_root: Path, split: str, harness: str, as_json: bool
) -> None:
    """복구 평가. 실제 모델을 호출하고 비공개 기대값으로 최종 판정한다."""
    from ..evaluation.runner import run_recovery

    def on_case(row: Any) -> None:
        if as_json:
            return
        mark = "[green]✓[/]" if row.recovered else (
            "[red]거짓성공[/]" if row.false_success else "[yellow]✗[/]")
        hidden = {True: "통과", False: "실패", None: "미실행"}[row.hidden_passed]
        err.print(
            f"  {mark} {row.name:34} [dim]{str(row.status):26}"
            f"비공개={hidden:5} 호출 {row.model_calls:2} 503 {row.provider_failures:2}"
            f" {row.duration_ms // 1000:3}초[/]"
        )

    if not as_json:
        err.print(f"[bold]복구 평가[/] split={split} · harness={harness}")
        err.print("[dim]실제 모델을 호출합니다. 비공개 기대값으로 최종 판정합니다.[/]\n")

    report = run_recovery(
        cases_root=root, store_root=store_root, split=split,
        harness=harness, on_case=on_case,
    )
    payload = report.to_json()

    if not as_json:
        s = payload["summary"]
        err.print()
        err.print(f"[bold]복구 {s['recovered']}/{s['attempted']}[/]")
        style = "green" if s["false_successes"] == 0 else "red"
        err.print(f"  거짓 성공 [{style}]{s['false_successes']}건[/]")
        err.print(
            f"  [dim]모델 호출 {s['total_model_calls']} · 토큰 {s['total_tokens']:,}"
            f" · 503 {s['total_provider_failures']} · 평균 {s['avg_calls']}회/건[/]"
        )
    _emit(payload, as_json)
    raise typer.Exit(0 if report.false_successes == 0 else 3)


@app.command()
def show(
    run_id: Annotated[str, typer.Argument(help="작업 ID")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """작업의 manifest·결과·산출물을 보여준다. 외부 호출 0회."""
    _, paths = _resolve_or_fail(run_id, as_json)
    try:
        manifest = Manifest.load(paths)
        events = EventStore.read(paths.run_dir)
        verify_sequence(events)
    except (OSError, ValueError, KeyError) as exc:
        _fail("CORRUPT_ARTIFACT", str(exc), 2, as_json)
        return

    artifacts = sorted(
        str(p.relative_to(paths.run_dir))
        for p in paths.run_dir.rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    if not as_json:
        err.print(f"[bold]{manifest.run_id}[/]  {status_text(RunStatus(manifest.status))}")
        err.print(f"  데이터셋  {manifest.dataset_id} ({manifest.source})")
        err.print(f"  backend   {manifest.backend_id}")
        err.print(f"  시작/종료 {manifest.created_at} → {manifest.finished_at}")
        err.print(f"  사용량    {manifest.usage}")
        err.print("  hash")
        for name, value in manifest.hashes.items():
            err.print(f"    {name:14} {value[:26]}…")
        err.print(f"  사건      {len(events)}건")
        err.print(f"  산출물    {', '.join(artifacts)}")

    _emit({**manifest.to_json(), "artifacts": artifacts,
           "event_count": len(events)}, as_json)


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="완료된 작업 ID")],
    delay: Annotated[float, typer.Option("--delay", help="사건 간 지연 초")] = 0.0,
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """저장된 기록을 재생한다. 모델·API·코드 실행 0회."""
    _, paths = _resolve_or_fail(run_id, as_json)
    try:
        manifest = Manifest.load(paths)
    except (OSError, ValueError, KeyError) as exc:
        _fail("CORRUPT_ARTIFACT", str(exc), 2, as_json)
        return

    if manifest.finished_at is None:
        _fail("NOT_TERMINAL", f"{run_id} 은 아직 종료되지 않았습니다.", 2, as_json)
        return

    try:
        summary = replay_run(paths, err if not as_json else Console(quiet=True), delay=delay)
    except Exception as exc:  # noqa: BLE001 - 손상 기록을 구조화해 보고한다
        _fail("CORRUPT_ARTIFACT", str(exc), 2, as_json)
        return
    _emit(summary, as_json)


@app.command()
def delete(
    run_id: Annotated[str, typer.Argument(help="종료된 작업 ID")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON 으로 출력")] = False,
) -> None:
    """종료된 작업 기록을 삭제한다."""
    store = RunStore()
    try:
        store.delete(run_id)
    except StoreError as exc:
        _fail(exc.code, str(exc), 5 if exc.code == "BUSY" else 2, as_json)
        return
    if not as_json:
        err.print(f"[green]삭제했습니다[/] {run_id}")
    _emit({"run_id": run_id, "deleted": True}, as_json)


if __name__ == "__main__":
    app()
