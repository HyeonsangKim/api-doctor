"""baseline → 복구 루프 → 보고서를 잇는 최상위 실행 (FR-015).

모델 경로가 준비되지 않았으면 baseline 결과를 그대로 유지하고
"복구를 시도하지 않았다"고 정직하게 남긴다.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model.gateway import (
    ChatBackend, ModelConfig, ModelGateway, ModelUnavailable, NvidiaChatBackend,
)
from ..profiling.nat import write_all as write_profile
from ..report.build import ReportInputs, write as write_report
from ..sandbox.base import SandboxLimits
from .baseline import BaselineResult, run_baseline
from .budget import BudgetLedger, Cancelled
from .events import EventType, RunStatus, Stage
from .deep_loop import orchestrate_deep
from .orchestrator import OrchestrationResult, orchestrate
from .store import RunStore, utc_iso


@dataclass(slots=True)
class RecoveryResult:
    baseline: BaselineResult
    status: RunStatus
    orchestration: OrchestrationResult | None = None
    report_paths: tuple[Path, Path] | None = None
    model_available: bool = True
    model_error: str | None = None
    artifacts: list[str] = field(default_factory=list)


def recover(
    *,
    dataset_id: str,
    code_path: Path,
    snapshot_id: str | None = None,
    source: str = "fixture",
    store: RunStore | None = None,
    registry_root: Path | None = None,
    limits: SandboxLimits | None = None,
    model_backend: "ChatBackend | None" = None,
    model_config: ModelConfig | None = None,
    scripted: bool = False,
    harness: str = "deepagents",
    on_event: Any = None,
) -> RecoveryResult:
    """전체 복구를 수행한다."""
    ledger = BudgetLedger()
    baseline = run_baseline(
        dataset_id=dataset_id, code_path=code_path, snapshot_id=snapshot_id,
        source=source, store=store, registry_root=registry_root,
        limits=limits, ledger=ledger, on_event=on_event,
    )
    session = baseline.session
    assert session is not None

    # baseline 에서 이미 끝났으면 (정상 코드·정책 차단) 복구를 시도하지 않는다.
    if not baseline.needs_repair:
        paths = _finalize(
            session, baseline.status, None, ledger, scripted,
            baseline_verdict=baseline.verdict,
        )
        return RecoveryResult(
            baseline=baseline, status=baseline.status, report_paths=paths,
            artifacts=_artifacts(session),
        )

    # 모델 경로 확보.
    # backend 를 주입받되 gateway 는 **여기서** 만든다 — 원장이 하나여야
    # "모든 모델 호출이 동일 gateway 를 통과" 한다는 FR-003 이 성립한다.
    config = model_config or ModelConfig()
    if model_backend is not None:
        gateway = ModelGateway(backend=model_backend, ledger=ledger, config=config)
    else:
        try:
            backend = NvidiaChatBackend(config, cancel_token=ledger.cancel)
            gateway = ModelGateway(backend=backend, ledger=ledger, config=config)
        except ModelUnavailable as exc:
            # 종료 표시는 `_finalize` 가 한 번만 남긴다.
            session.events.append(
                EventType.PLAN_REVISED,
                f"복구를 시도하지 않았습니다: {exc}",
                reason="model_unavailable",
            )
            paths = _finalize(
                session, RunStatus.NEEDS_USER_ACTION, None, ledger, scripted,
                baseline_verdict=baseline.verdict,
            )
            return RecoveryResult(
                baseline=baseline, status=RunStatus.NEEDS_USER_ACTION,
                report_paths=paths, model_available=False, model_error=str(exc),
                artifacts=_artifacts(session),
            )

    gateway.on_call = lambda call: session.events.append(
        EventType.MODEL_CALL,
        f"{call.agent_id} · {call.latency_ms}ms",
        **call.to_json(),
    )

    runner = orchestrate_deep if harness == "deepagents" else orchestrate
    session.events.append(
        EventType.STAGE_CHANGED, f"하네스: {harness}",
        harness=harness,
    )
    with _cancellation(ledger):
        try:
            orchestration = runner(session=session, model=gateway)
            status = orchestration.status
        except Cancelled:
            session.forced_halt = str(RunStatus.CANCELLED)
            orchestration = None
            status = RunStatus.CANCELLED

    paths = _finalize(
        session, status, orchestration, ledger, scripted, gateway,
        baseline_verdict=baseline.verdict,
        tool_log=orchestration.tool_log if orchestration else [],
    )
    return RecoveryResult(
        baseline=baseline, status=status, orchestration=orchestration,
        report_paths=paths, artifacts=_artifacts(session),
    )


class _cancellation:
    """Ctrl-C 를 CancelToken 으로 옮긴다 (AC-08)."""

    def __init__(self, ledger: BudgetLedger) -> None:
        self.ledger = ledger
        self.previous: Any = None

    def __enter__(self) -> "_cancellation":
        def handler(signum: int, frame: Any) -> None:
            self.ledger.cancel.set()

        try:
            self.previous = signal.signal(signal.SIGINT, handler)
        except ValueError:
            self.previous = None   # 메인 스레드가 아니면 설치하지 않는다
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.previous is not None:
            try:
                signal.signal(signal.SIGINT, self.previous)
            except ValueError:
                pass


def _finalize(
    session: Any,
    status: RunStatus,
    orchestration: OrchestrationResult | None,
    ledger: BudgetLedger,
    scripted: bool,
    gateway: ModelGateway | None = None,
    baseline_verdict: Any | None = None,
    tool_log: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path]:
    ledger.terminate()
    per_role = gateway.per_role_calls() if gateway else {}
    report_inputs = ReportInputs(
        session=session, status=status,
        gate=orchestration.decision if orchestration else None,
        delegations=orchestration.delegations if orchestration else [],
        model_calls_per_role=per_role or ledger.usage()["per_role_calls"],
        scripted=scripted, baseline_verdict=baseline_verdict,
    )
    paths = write_report(report_inputs)

    # FR-018: 역할별 호출·토큰·지연과 도구 구간을 산출한다.
    if gateway is not None:
        write_profile(
            session.paths.run_dir,
            model_calls=gateway.calls,
            tool_log=tool_log or [],
            ledger_usage=ledger.usage(),
        )

    from .store import Manifest

    manifest = Manifest.load(session.paths)
    manifest.status = str(status)
    manifest.stage = str(Stage.FINALIZING)
    manifest.finished_at = utc_iso()
    manifest.usage = {**ledger.usage(), "per_role_calls": per_role or
                      ledger.usage()["per_role_calls"]}
    manifest.hashes["final_candidate"] = session.current_hash
    manifest.save(session.paths, session.events)

    session.events.append(
        EventType.RUN_FINISHED,
        f"작업을 종료했습니다: {status}",
        status=str(status), model_calls=ledger.usage()["model_calls"],
        report="report.md",
    )
    return paths


def _artifacts(session: Any) -> list[str]:
    return sorted(
        str(p.relative_to(session.paths.run_dir))
        for p in session.paths.run_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )
