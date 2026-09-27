"""prepare → baseline 경로 (FR-005, FR-006, AC-05).

M1 범위: 모델을 호출하지 않는다. 원본이 이미 계약을 만족하면
`verified_unchanged` 로 끝나고, 결함이 있으면 `delegating` 으로 넘길
준비를 마친 상태로 멈춘다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..data.broker import DataBroker, Snapshot, SnapshotInvalid
from ..registry.loader import Dataset, RegistryInvalid, load_registry
from ..sandbox.base import Denial, SandboxLimits, SandboxOutcome
from ..sandbox.selftest import BackendSelection, RuntimeUnavailable, select_backend  # noqa: F401
from ..verify.classify import Classification, classify_run
from ..verify.verifier import FixedVerifier, Verdict, load_verifier
from .budget import BudgetLedger
from .evidence import EvidenceStore
from .events import EventStore, EventType, RunStatus, Stage
from .session import RunSession
from .store import Manifest, RunPaths, RunStore, acquire_lock, release_lock, utc_iso

MAX_CODE_BYTES = 100 * 1024
MAX_QUERY_BYTES = 8 * 1024
_SECRET_HINTS = ("api_key", "apikey", "secret", "password", "authorization", "token")


class InputError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class BaselineResult:
    run_id: str
    status: RunStatus
    verdict: Verdict | None
    outcome: SandboxOutcome | None
    paths: RunPaths
    manifest: Manifest
    needs_repair: bool = False
    error: str | None = None
    denials: list[Denial] = field(default_factory=list)
    session: "RunSession | None" = None
    selection: "BackendSelection | None" = None
    snapshot: "Snapshot | None" = None
    dataset: "Dataset | None" = None


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_input(code_path: Path, query: dict[str, Any]) -> None:
    """CLI 입력 검사 (FR-005). 코드 실행 전에 전부 수행한다."""
    if not code_path.is_file():
        raise InputError("INVALID_INPUT", f"연결 파일을 찾을 수 없습니다: {code_path}")
    if code_path.suffix != ".py":
        raise InputError("UNSUPPORTED_INPUT", "Python 파일 1개만 지원합니다.")

    size = code_path.stat().st_size
    if size > MAX_CODE_BYTES:
        raise InputError(
            "INVALID_INPUT",
            f"연결 파일이 {MAX_CODE_BYTES // 1024}KB 상한을 넘습니다 ({size // 1024}KB).",
        )

    source = code_path.read_text(encoding="utf-8", errors="replace")
    if "def fetch_records" not in source:
        raise InputError(
            "UNSUPPORTED_INPUT",
            "지원 인터페이스는 fetch_records(http, query) 입니다.",
        )

    # 원문 키가 보이면 전송 없이 입력 수정을 안내한다 (PRD §4.3).
    lowered = source.lower()
    for hint in _SECRET_HINTS:
        for line in lowered.splitlines():
            if hint in line and "=" in line and any(c.isalnum() for c in line.split("=", 1)[1]):
                stripped = line.split("=", 1)[1].strip().strip("\"'")
                if len(stripped) >= 16 and stripped.isalnum():
                    raise InputError(
                        "INVALID_INPUT",
                        f"코드에 비밀값으로 보이는 문자열이 있습니다 ({hint}). "
                        "모델에 전송하지 않았습니다. 환경변수로 옮기고 다시 실행하세요.",
                    )

    if len(json.dumps(query).encode()) > MAX_QUERY_BYTES:
        raise InputError("INVALID_INPUT", "query 가 8KB 상한을 넘습니다.")


def run_baseline(
    *,
    dataset_id: str,
    code_path: Path,
    snapshot_id: str | None = None,
    source: str = "fixture",
    store: RunStore | None = None,
    registry_root: Path | None = None,
    limits: SandboxLimits | None = None,
    ledger: "BudgetLedger | None" = None,
    on_event: Any = None,
) -> BaselineResult:
    """입력 검사 → 격리 검증 → 자료 동결 → baseline 판정."""
    store = store or RunStore()
    limits = limits or SandboxLimits()
    ledger = ledger or BudgetLedger()

    # ---- 레지스트리 (코드 실행 전) ----
    try:
        registry = load_registry(registry_root)
    except RegistryInvalid as exc:
        raise InputError("REGISTRY_INVALID", str(exc)) from exc
    dataset = registry.get(dataset_id)
    if dataset is None:
        raise InputError(
            "INVALID_INPUT",
            f"등록되지 않은 dataset_id: {dataset_id} (등록: {sorted(registry)})",
        )

    if source == "live" and not dataset.live_ready:
        raise InputError(
            "UNSUPPORTED_INPUT",
            "live 수집이 준비되지 않았습니다: " + "; ".join(dataset.live_blockers),
        )
    if source != "fixture":
        raise InputError("UNSUPPORTED_INPUT", f"지원하지 않는 source: {source}")

    validate_input(code_path, dataset.contract.query)

    snapshot = _load_snapshot(dataset, snapshot_id)

    # ---- 격리 backend (후보를 만지기 전) ----
    try:
        selection = select_backend(limits)
    except RuntimeUnavailable as exc:
        raise InputError("RUNTIME_UNAVAILABLE", str(exc)) from exc

    # ---- 작업 폴더 ----
    run_id, paths = store.create()
    acquire_lock(paths)
    events = EventStore(paths.run_dir)
    if on_event is not None:
        events.subscribe(on_event)

    try:
        return _execute(
            run_id=run_id, paths=paths, events=events, dataset=dataset,
            snapshot=snapshot, selection=selection, code_path=code_path,
            limits=limits, source=source, ledger=ledger,
        )
    finally:
        release_lock(paths)


def _load_snapshot(dataset: Dataset, snapshot_id: str | None) -> Snapshot:
    if snapshot_id is None:
        available = sorted(p.stem for p in (dataset.root / "snapshots").glob("*.json"))
        if not available:
            raise InputError("SNAPSHOT_REQUIRED", "동결 스냅샷이 없습니다.")
        # 여러 개면 추측하지 않는다. 레지스트리가 기본을 명시해야 한다.
        snapshot_id = dataset.default_snapshot
        if snapshot_id is None:
            if len(available) > 1:
                raise InputError(
                    "SNAPSHOT_REQUIRED",
                    f"기본 스냅샷이 등록되지 않았습니다. 지정하세요: {available}",
                )
            snapshot_id = available[0]
    try:
        return Snapshot.load(dataset.snapshot_path(snapshot_id), dataset.dataset_id)
    except SnapshotInvalid as exc:
        raise InputError("INVALID_INPUT", str(exc)) from exc


def _execute(
    *,
    run_id: str,
    paths: RunPaths,
    events: EventStore,
    dataset: Dataset,
    snapshot: Snapshot,
    selection: BackendSelection,
    code_path: Path,
    limits: SandboxLimits,
    source: str,
    ledger: "BudgetLedger",
) -> BaselineResult:
    # 원본을 읽기 전용 복사한다. 원본은 import 하지도 실행하지도 않는다.
    original = paths.candidates / "original.py"
    original.write_bytes(code_path.read_bytes())
    original.chmod(0o600)   # register_candidate 가 다시 쓰고 0400 으로 잠근다
    original_hash = sha256_file(original)

    hashes = {
        "dataset": dataset.dataset_hash,
        "contract": dataset.contract.contract_hash,
        "probe_catalog": dataset.probes.catalog_hash,
        "snapshot": snapshot.snapshot_hash,
        "original": original_hash,
    }
    manifest = Manifest(
        run_id=run_id, dataset_id=dataset.dataset_id, source=source,
        status="running", stage=str(Stage.PREPARE), created_at=utc_iso(),
        backend_id=str(selection.backend.backend_id), hashes=hashes,
        limits={"sandbox": asdict(limits), "broker_calls": 20},
        owner_uid=os.getuid(),
    )
    manifest.save(paths, events)

    events.append(
        EventType.RUN_STARTED,
        f"{dataset.display_name} baseline 을 시작합니다.",
        dataset_id=dataset.dataset_id, source=source,
        backend_id=str(selection.backend.backend_id), hashes=hashes,
        snapshot_source_kind=snapshot.source_kind,
    )
    events.append(
        EventType.STAGE_CHANGED, "격리 경계를 확인했습니다.",
        **{"from": str(Stage.PREPARE), "to": str(Stage.BASELINE),
           "boundary_cases": [
               {"case": str(r.case), "blocked": r.blocked}
               for r in selection.report.results]},
    )

    # ---- 원본 격리 실행 ----
    manifest.stage = str(Stage.BASELINE)
    broker = DataBroker(dataset=dataset, snapshot=snapshot)
    outcome = selection.backend.run_candidate(
        original, dataset.contract.query, broker.respond, limits
    )
    ledger.spend_sandbox_run()
    for _ in range(broker.call_count):
        try:
            ledger.spend_broker_call()
        except Exception:  # noqa: BLE001 - 상한은 다음 판정에서 걸린다
            break
    events.append(
        EventType.SANDBOX_RUN,
        f"원본을 격리 실행했습니다 ({outcome.duration_ms}ms).",
        backend_id=str(outcome.backend_id), exit_code=outcome.exit_code,
        returned=len(outcome.records) if outcome.records is not None else None,
        broker_calls=broker.usage(), denial_count=len(outcome.denials),
        candidate_hash=original_hash,
    )
    _record_denials(paths, events, outcome.denials)

    # ---- 공급자 응답 분류 (고정 검증보다 먼저) ----
    # PRD §3.3: 신뢰된 시스템은 인증·정책·예산 오류를 모델 판단보다 먼저 중단한다.
    # 인증이 막힌 응답으로 계약을 검증하는 것은 의미가 없으므로 건너뛴다.
    provider = classify_run(broker.served_bodies(), dataset.provider_errors)
    if provider.classification is not Classification.OK:
        events.append(
            EventType.BASELINE_RESULT,
            f"공급자 응답 분류: {provider.classification}"
            + (f" ({provider.code})" if provider.code else ""),
            **provider.to_json(),
        )

    # 검증기는 세션이 항상 필요로 하므로 분류와 무관하게 만든다.
    verifier: FixedVerifier = load_verifier(dataset.contract, dataset.expected_path())

    verdict: Verdict | None = None
    if not provider.classification.is_blocking:
        verdict = verifier.verify(
            records=outcome.records, execution_error=outcome.error,
            candidate_hash=original_hash, snapshot_hash=snapshot.snapshot_hash,
        )
        _write_check(paths, verdict)
        for result in verdict.results:
            events.append(
                EventType.CHECK_RESULT, f"{result.kind}: {result.summary}",
                kind=str(result.kind), outcome=str(result.outcome),
                candidate_hash=original_hash, contract_hash=verdict.contract_hash,
                snapshot_hash=verdict.snapshot_hash, metrics=result.metrics,
            )
        events.append(
            EventType.BASELINE_RESULT,
            "원본이 계약을 만족합니다." if verdict.passed else "원본에 결함이 있습니다.",
            passed=verdict.passed,
            failed_checks=[str(r.kind) for r in verdict.failures],
        )

    # ---- 판정 ----
    if verdict is not None and verdict.passed:
        status = RunStatus.VERIFIED_UNCHANGED
        needs_repair = False
        summary = "원본이 이미 계약을 만족하여 모델 호출 없이 종료합니다."
    elif provider.classification.is_blocking:
        # 코드를 고쳐서 해결될 문제가 아니다. 모델을 부르지 않는다 (AC-06).
        status = (
            RunStatus.NEEDS_USER_ACTION
            if provider.classification is Classification.AUTH
            else RunStatus.BUDGET_EXHAUSTED
            if provider.classification is Classification.QUOTA
            else RunStatus.EXTERNAL_UNAVAILABLE
        )
        needs_repair = False
        summary = (
            f"공급자 응답이 {provider.classification} 로 분류되었습니다 "
            f"({provider.code}). 코드 수리 없이 종료합니다."
        )
    elif any(d.is_policy_violation for d in outcome.denials) and outcome.records is None:
        status = RunStatus.POLICY_BLOCKED
        needs_repair = False
        summary = "격리 정책이 후보의 동작을 차단했습니다."
    else:
        status = RunStatus.VERIFICATION_FAILED
        needs_repair = True
        summary = "결함이 확인되었습니다. 여기서부터 전문 에이전트 위임이 필요합니다 (M2)."

    manifest.stage = str(Stage.FINALIZING)
    manifest.status = str(status)
    manifest.finished_at = utc_iso()
    manifest.usage = {
        "model_calls": 0, "tokens": 0, "tokens_kind": "actual",
        "broker_calls": broker.usage(), "sandbox_runs": 1,
    }
    manifest.save(paths, events)
    # 종료 표시는 호출자(`recover`)가 한 번만 남긴다.
    # 여기서도 남기면 events.jsonl 에 run_finished 가 두 번 찍혀
    # replay 와 기록 무결성이 어긋난다.
    events.append(
        EventType.BASELINE_RESULT, summary,
        status=str(status), model_calls=0, needs_repair=needs_repair,
    )

    session = RunSession(
        run_id=run_id, paths=paths, events=events, ledger=ledger,
        evidence=EvidenceStore(paths.run_dir), dataset=dataset, snapshot=snapshot,
        backend=selection.backend, verifier=verifier, limits=limits,
    )
    session.register_candidate(
        source=original.read_text(encoding="utf-8"), version=0,
        base_hash=None, author="operator",
    )
    session.denials.extend(outcome.denials)
    session.last_broker_usage = broker.usage()

    return BaselineResult(
        run_id=run_id, status=status, verdict=verdict, outcome=outcome,
        paths=paths, manifest=manifest, needs_repair=needs_repair,
        error=outcome.error, denials=list(outcome.denials),
        session=session, selection=selection, snapshot=snapshot, dataset=dataset,
    )


def _record_denials(paths: RunPaths, events: EventStore, denials: list[Denial]) -> None:
    if not denials:
        return
    with open(paths.denials, "a", encoding="utf-8") as fh:
        for denial in denials:
            fh.write(json.dumps(denial.to_json(), ensure_ascii=False) + "\n")
    paths.denials.chmod(0o600)
    for denial in denials:
        events.append(
            EventType.POLICY_DENIED, denial.reason[:160],
            **denial.to_json(),
        )


def _write_check(paths: RunPaths, verdict: Verdict) -> None:
    """검사 결과를 3-hash 조합으로 키를 잡아 저장한다 (AC-13)."""
    key = hashlib.sha256(
        f"{verdict.candidate_hash}|{verdict.contract_hash}|{verdict.snapshot_hash}".encode()
    ).hexdigest()[:16]
    path = paths.checks / f"check_{key}.json"
    path.write_text(
        json.dumps(verdict.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
