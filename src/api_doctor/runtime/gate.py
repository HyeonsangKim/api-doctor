"""종료 게이트 (FR-012, AC-04, AC-09, 아키텍처 §9).

우회할 수 없는 지점이다. main 의 종료 요청·일반 텍스트 종료·agent 예외·
루프 상한이 전부 여기로 수렴한다.

판정 함수는 **불리언들의 논리곱**이며 LLM 출력을 입력으로 받지 않는다.
모델의 "괜찮습니다"는 이 함수 어디에도 인자가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..verify.probes import ProbeResult, coverage_of
from ..verify.verifier import Outcome, Verdict
from .events import RunStatus
from .session import AuditRecord, RunSession


class GateRejection(StrEnum):
    """게이트가 main 에게 돌려보내는 사유."""

    STALE = "STALE"
    MISSING_AUDIT = "MISSING_AUDIT"
    UNRESOLVED_FINDING = "UNRESOLVED_FINDING"


@dataclass(frozen=True, slots=True)
class AuditCompleteness:
    """감사 완료 여부의 근거를 전부 드러낸다 (PRD §3.2)."""

    complete: bool
    required_risks: tuple[str, ...]
    covered_risks: tuple[str, ...]
    uncovered_risks: tuple[str, ...]
    risks_without_record: tuple[str, ...]
    ran_non_baseline_probe: bool
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "required_risks": list(self.required_risks),
            "covered_risks": list(self.covered_risks),
            "uncovered_risks": list(self.uncovered_risks),
            "risks_without_record": list(self.risks_without_record),
            "ran_non_baseline_probe": self.ran_non_baseline_probe,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GateDecision:
    """게이트 1회 평가의 결과."""

    status: RunStatus | None            # None 이면 main 에게 돌려보낸다
    rejection: GateRejection | None
    verdict: Verdict | None
    audit: AuditCompleteness | None
    unresolved: tuple[AuditRecord, ...] = ()
    detail: str = ""
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": str(self.status) if self.status else None,
            "rejection": str(self.rejection) if self.rejection else None,
            "detail": self.detail,
            "checks": self.checks,
            "verdict": self.verdict.to_json() if self.verdict else None,
            "audit": self.audit.to_json() if self.audit else None,
            "unresolved": [
                {"risk_id": r.risk_id, "conclusion": r.conclusion,
                 "hypothesis": r.hypothesis[:200]}
                for r in self.unresolved
            ],
        }


def audit_completeness(session: RunSession, candidate_hash: str) -> AuditCompleteness:
    """PRD §3.2 의 런타임 확인 조건을 그대로 검사한다.

    빈 findings·찬성 문구만으로는 완료되지 않는다. 실제 probe 실행과
    고정 coverage metadata 로만 판정한다.
    """
    required = tuple(a.risk_id for a in session.dataset.contract.audit_requirements)
    results: list[ProbeResult] = session.executed_probes_for(candidate_hash)

    catalog = session.dataset.probes
    non_baseline = [
        r for r in results
        if (probe := catalog.get(r.probe_id)) is not None
        and not probe.is_baseline
        and r.outcome.is_evidence
    ]
    covered = coverage_of(results)
    uncovered = tuple(risk for risk in required if risk not in covered)

    records = session.audit_for(candidate_hash)
    recorded_risks = {r.risk_id for r in records}
    without_record = tuple(risk for risk in required if risk not in recorded_risks)

    reasons: list[str] = []
    if not non_baseline:
        reasons.append(
            "baseline 과 다른 probe 를 현재 후보에서 한 번도 실행하지 않았습니다"
        )
    if uncovered:
        reasons.append(f"증거로 덮이지 않은 위험 영역: {list(uncovered)}")
    if without_record:
        reasons.append(f"감사 기록이 없는 위험 영역: {list(without_record)}")

    return AuditCompleteness(
        complete=not reasons,
        required_risks=required,
        covered_risks=tuple(sorted(covered)),
        uncovered_risks=uncovered,
        risks_without_record=without_record,
        ran_non_baseline_probe=bool(non_baseline),
        reason="; ".join(reasons),
    )


def evaluate(
    session: RunSession,
    *,
    candidate_hash: str,
    final_verdict: Verdict | None,
) -> GateDecision:
    """최종 판정. 순서가 곧 우선순위다 (아키텍처 §9).

    1. 강제 중단이 있었나 — 승계 불가
    2. 현재 hash 의 고정 검증을 통과했나
    3. 현재 hash 에 대한 감사가 완료됐나
    4. 계약 관련 미해결 근거가 없나
    """
    checks: dict[str, bool] = {}

    # ① 강제 중단은 무엇으로도 뒤집지 못한다.
    if session.forced_halt is not None:
        checks["no_forced_halt"] = False
        return GateDecision(
            status=RunStatus(session.forced_halt), rejection=None,
            verdict=final_verdict, audit=None, checks=checks,
            detail="강제 중단이 발생해 부분 결과만 확정합니다. "
                   "완료된 검사가 있어도 verified 로 승격하지 않습니다.",
        )
    checks["no_forced_halt"] = True

    # ② 고정 검증. 반드시 현재 후보의 것이어야 한다.
    if final_verdict is None:
        checks["fixed_check_passed"] = False
        return GateDecision(
            status=None, rejection=GateRejection.STALE, verdict=None, audit=None,
            checks=checks, detail="현재 후보의 종료 검증이 실행되지 않았습니다.",
        )
    if final_verdict.candidate_hash != candidate_hash:
        checks["fixed_check_passed"] = False
        return GateDecision(
            status=None, rejection=GateRejection.STALE, verdict=final_verdict,
            audit=None, checks=checks,
            detail=f"검증 결과가 다른 후보의 것입니다 "
                   f"({final_verdict.candidate_hash[:15]}… != {candidate_hash[:15]}…).",
        )

    checks["fixed_check_passed"] = final_verdict.passed
    if not final_verdict.passed:
        inconclusive = any(
            r.outcome is Outcome.INCONCLUSIVE for r in final_verdict.results
        )
        return GateDecision(
            status=(RunStatus.VERIFICATION_INCONCLUSIVE if inconclusive
                    else RunStatus.VERIFICATION_FAILED),
            rejection=None, verdict=final_verdict, audit=None, checks=checks,
            detail="; ".join(r.summary for r in final_verdict.failures)[:400],
        )

    # ③ 감사 완료. 모델의 찬성이 아니라 실행된 probe 로 판정한다.
    audit = audit_completeness(session, candidate_hash)
    checks["audit_complete"] = audit.complete
    if not audit.complete:
        return GateDecision(
            status=None, rejection=GateRejection.MISSING_AUDIT,
            verdict=final_verdict, audit=audit, checks=checks, detail=audit.reason,
        )

    # ④ 계약 관련 미해결 근거. 해소되지 않았으면 보류한다.
    unresolved = tuple(session.unresolved_contract_findings(candidate_hash))
    checks["no_unresolved_findings"] = not unresolved
    if unresolved:
        return GateDecision(
            status=RunStatus.VERIFICATION_INCONCLUSIVE,
            rejection=GateRejection.UNRESOLVED_FINDING, verdict=final_verdict,
            audit=audit, unresolved=unresolved, checks=checks,
            detail="해소되지 않은 감사 항목이 있어 성공으로 승격하지 않습니다: "
                   + ", ".join(f"{r.risk_id}={r.conclusion}" for r in unresolved),
        )

    return GateDecision(
        status=RunStatus.VERIFIED_REPAIRED, rejection=None, verdict=final_verdict,
        audit=audit, checks=checks,
        detail="고정 검증 통과 · 감사 완료 · 미해결 항목 없음.",
    )
