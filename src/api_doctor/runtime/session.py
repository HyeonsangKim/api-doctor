"""RunSession — Zone T 의 실행 상태 (아키텍처 §3).

도구가 조작하는 대상은 전부 여기 모여 있다. 에이전트는 이 객체를 직접
받지 않고, 도구 게이트웨이가 바인딩한 클로저를 통해서만 닿는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..data.broker import DataBroker, Snapshot
from ..registry.loader import Dataset, Probe
from ..sandbox.base import Denial, SandboxBackend, SandboxLimits
from ..verify.probes import ProbeResult, ProbeRunner, UnsupportedProbe
from ..verify.verifier import FixedVerifier, Verdict
from .budget import BudgetLedger
from .evidence import EvidenceStore
from .events import EventStore, EventType
from .store import RunPaths


@dataclass(slots=True)
class Candidate:
    """후보 코드 1버전."""

    version: int          # 0 = 원본, 1·2 = 패치
    path: Path
    candidate_hash: str
    base_hash: str | None
    author: str

    @property
    def is_original(self) -> bool:
        return self.version == 0


@dataclass(slots=True)
class AuditRecord:
    """감사자가 특정 후보에 대해 반환한 내용 (PRD §3.2 감사 완료 조건)."""

    candidate_hash: str
    risk_id: str
    hypothesis: str
    invariant: str
    evidence_ids: tuple[str, ...]
    probe_result_ids: tuple[str, ...]
    conclusion: str       # no_issue | issue | inconclusive


@dataclass(slots=True)
class RunSession:
    """한 run 의 Zone T 상태 전부."""

    run_id: str
    paths: RunPaths
    events: EventStore
    ledger: BudgetLedger
    evidence: EvidenceStore
    dataset: Dataset
    snapshot: Snapshot
    backend: SandboxBackend
    verifier: FixedVerifier
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    candidates: dict[str, Candidate] = field(default_factory=dict)
    current_hash: str = ""
    probe_results: dict[str, ProbeResult] = field(default_factory=dict)
    audit_records: list[AuditRecord] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    final_verdict: Verdict | None = None
    forced_halt: str | None = None
    last_broker_usage: dict[str, Any] = field(default_factory=dict)
    # 최근 실행에서 broker 가 처리한 요청·응답. `inspect_trace` 가 쓴다.
    last_broker_trace: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- 후보 관리

    def register_candidate(
        self, *, source: str, version: int, base_hash: str | None, author: str
    ) -> Candidate:
        digest = "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
        name = "original.py" if version == 0 else f"v{version}.py"
        path = self.paths.candidates / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o400)
        candidate = Candidate(
            version=version, path=path, candidate_hash=digest,
            base_hash=base_hash, author=author,
        )
        self.candidates[digest] = candidate
        self.current_hash = digest
        return candidate

    @property
    def current(self) -> Candidate:
        return self.candidates[self.current_hash]

    @property
    def original(self) -> Candidate:
        return next(c for c in self.candidates.values() if c.is_original)

    def source_of(self, candidate_hash: str) -> str:
        candidate = self.candidates.get(candidate_hash)
        if candidate is None:
            raise KeyError(candidate_hash)
        return candidate.path.read_text(encoding="utf-8")

    # ------------------------------------------------------------- 실행

    def execute_candidate(
        self, candidate_hash: str, query: dict[str, Any], *, is_finish: bool = False
    ) -> tuple[list[dict[str, Any]] | None, str | None, set[int], int, int]:
        """후보 1회 실행. 예산을 차감하고 denial 을 수집한다."""
        candidate = self.candidates.get(candidate_hash)
        if candidate is None:
            raise KeyError(f"등록되지 않은 후보입니다: {candidate_hash}")

        self.ledger.spend_sandbox_run(is_finish=is_finish)
        broker = DataBroker(
            dataset=self.dataset, snapshot=self.snapshot,
            max_calls=self.ledger.limits.broker_calls - self.ledger.broker_calls,
        )
        outcome = self.backend.run_candidate(
            candidate.path, query, broker.respond, self.limits
        )
        for _ in range(broker.call_count):
            try:
                self.ledger.spend_broker_call()
            except Exception:  # noqa: BLE001 - 상한 초과는 다음 판정에서 걸린다
                break

        self.denials.extend(outcome.denials)
        self._append_denials(outcome.denials)
        self.last_broker_usage = broker.usage()
        self.last_broker_trace = broker.trace()

        self.events.append(
            EventType.SANDBOX_RUN,
            f"후보를 격리 실행했습니다 ({outcome.duration_ms}ms).",
            backend_id=str(outcome.backend_id), exit_code=outcome.exit_code,
            returned=len(outcome.records) if outcome.records is not None else None,
            candidate_hash=candidate_hash, query=query,
            broker=broker.usage(), is_finish=is_finish,
        )
        return (
            outcome.records, outcome.error, broker.requested_positions(),
            broker.call_count, outcome.duration_ms,
        )

    def probe_runner_for(self, candidate_hash: str) -> ProbeRunner:
        def execute(query: dict[str, Any]):
            return self.execute_candidate(candidate_hash, query)

        return ProbeRunner(self.dataset.contract, execute)

    def run_probe(self, probe_id: str, candidate_hash: str) -> ProbeResult:
        probe: Probe | None = self.dataset.probes.get(probe_id)
        if probe is None:
            raise UnsupportedProbe(f"등록되지 않은 probe 입니다: {probe_id}")

        result = self.probe_runner_for(candidate_hash).run(
            probe, candidate_hash=candidate_hash,
            snapshot_hash=self.snapshot.snapshot_hash,
        )
        self.probe_results[result.probe_result_id] = result
        self._write_check(result)
        return result

    # ------------------------------------------------------------- 감사 상태

    def audit_for(self, candidate_hash: str) -> list[AuditRecord]:
        """현재 후보에 대한 감사만. 패치가 바뀌면 이전 감사는 만료된다 (AC-13)."""
        return [r for r in self.audit_records if r.candidate_hash == candidate_hash]

    def executed_probes_for(self, candidate_hash: str) -> list[ProbeResult]:
        return [
            r for r in self.probe_results.values()
            if r.candidate_hash == candidate_hash
            and r.snapshot_hash == self.snapshot.snapshot_hash
        ]

    def unresolved_contract_findings(self, candidate_hash: str) -> list[AuditRecord]:
        return [
            r for r in self.audit_for(candidate_hash)
            if r.conclusion in ("issue", "inconclusive")
        ]

    # ------------------------------------------------------------- 기록

    def _append_denials(self, denials: list[Denial]) -> None:
        if not denials:
            return
        with open(self.paths.denials, "a", encoding="utf-8") as handle:
            for denial in denials:
                handle.write(json.dumps(denial.to_json(), ensure_ascii=False) + "\n")
        self.paths.denials.chmod(0o600)
        for denial in denials:
            self.events.append(
                EventType.POLICY_DENIED, denial.reason[:160], **denial.to_json()
            )

    def _write_check(self, result: ProbeResult) -> None:
        path = self.paths.checks / f"{result.probe_result_id}.json"
        path.write_text(
            json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def write_verdict(self, verdict: Verdict, label: str) -> None:
        key = hashlib.sha256(
            f"{verdict.candidate_hash}|{verdict.contract_hash}|{verdict.snapshot_hash}".encode()
        ).hexdigest()[:16]
        path = self.paths.checks / f"check_{label}_{key}.json"
        path.write_text(
            json.dumps(verdict.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
