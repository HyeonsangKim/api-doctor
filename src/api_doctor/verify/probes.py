"""probe 실행기 (FR-011, AC-04).

probe 의 불변식은 **비공개 기대값 없이 검사 가능**하다. 그래야 감사자가
정답을 보지 않고도 손실을 관측할 수 있다. 예를 들어 `partition_invariance`
는 "구간 크기를 바꿔도 같은 식별키 집합이 나와야 한다"이며, 정답이 몇 건인지
몰라도 어긋남이 드러난다.

이 모듈은 `agents` · `tools` · `model` 을 import 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from ..registry.loader import Contract, Probe


class ProbeOutcome(StrEnum):
    HOLDS = "holds"            # 불변식이 성립하며 실제 증거를 얻었다
    VIOLATED = "violated"      # 어긋남을 관측했다 — 이것도 증거다
    OBSERVED = "observed"      # 판정 없이 관측만 (baseline)
    VACUOUS = "vacuous"        # 공허하게 성립했다 — 증거가 아니다
    UNSUPPORTED = "unsupported"  # 검토된 응답이 없어 실행하지 않았다
    ERROR = "error"            # 후보가 실행되지 않았다

    @property
    def is_evidence(self) -> bool:
        """이 결과가 위험 영역을 실제로 덮는가.

        0건짜리 후보는 "구간을 바꿔도 같은 0건" 이라 분할 불변식이 공허하게
        성립한다. 그것을 통과로 세면 아무것도 안 하는 후보가 감사를 통과한다.
        """
        return self in (ProbeOutcome.HOLDS, ProbeOutcome.VIOLATED,
                        ProbeOutcome.OBSERVED)


class UnsupportedProbe(RuntimeError):
    """등록되지 않았거나 동결 자료가 없는 probe 입니다."""


@dataclass(slots=True)
class RunObservation:
    """probe 안의 실행 1회에 대한 관측."""

    params: dict[str, Any]
    record_count: int | None
    identity_values: tuple[str, ...]
    requested_positions: tuple[int, ...]
    broker_calls: int
    error: str | None
    duration_ms: int

    def to_json(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "record_count": self.record_count,
            "identity_values": list(self.identity_values),
            "requested_positions": list(self.requested_positions),
            "broker_calls": self.broker_calls,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class ProbeResult:
    probe_id: str
    invariant: str
    outcome: ProbeOutcome
    summary: str
    covers: tuple[str, ...]
    candidate_hash: str
    snapshot_hash: str
    observations: list[RunObservation] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def probe_result_id(self) -> str:
        return f"pr_{self.probe_id}_{self.candidate_hash[7:15]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "probe_result_id": self.probe_result_id,
            "probe_id": self.probe_id,
            "invariant": self.invariant,
            "outcome": str(self.outcome),
            "summary": self.summary,
            "covers": list(self.covers),
            "candidate_hash": self.candidate_hash,
            "snapshot_hash": self.snapshot_hash,
            "observations": [o.to_json() for o in self.observations],
            "detail": self.detail,
        }


# 후보 1회 실행을 수행하는 콜백. (query) -> (records, error, positions, broker_calls, ms)
ExecuteRun = Callable[[dict[str, Any]], tuple[
    list[dict[str, Any]] | None, str | None, set[int], int, int
]]


class ProbeRunner:
    """등록 probe 를 실행하고 불변식을 판정한다."""

    def __init__(self, contract: Contract, execute: ExecuteRun) -> None:
        self.contract = contract
        self.execute = execute

    def run(
        self, probe: Probe, *, candidate_hash: str, snapshot_hash: str
    ) -> ProbeResult:
        observations: list[RunObservation] = []
        for overrides in probe.runs:
            query = {**self.contract.query, **overrides}
            records, error, positions, broker_calls, duration_ms = self.execute(query)
            observations.append(
                RunObservation(
                    params=dict(overrides),
                    record_count=len(records) if records is not None else None,
                    identity_values=tuple(
                        str(r.get(self.contract.identity_key))
                        for r in (records or [])
                        if r.get(self.contract.identity_key) is not None
                    ),
                    requested_positions=tuple(sorted(positions)),
                    broker_calls=broker_calls,
                    error=error,
                    duration_ms=duration_ms,
                )
            )

        outcome, summary, detail = self._judge(probe, observations)
        return ProbeResult(
            probe_id=probe.probe_id,
            invariant=probe.invariant,
            outcome=outcome,
            summary=summary,
            covers=probe.covers,
            candidate_hash=candidate_hash,
            snapshot_hash=snapshot_hash,
            observations=observations,
            detail=detail,
        )

    # -------------------------------------------------------------- 불변식

    def _judge(
        self, probe: Probe, observations: list[RunObservation]
    ) -> tuple[ProbeOutcome, str, dict[str, Any]]:
        if any(o.error for o in observations):
            first = next(o for o in observations if o.error)
            return (
                ProbeOutcome.ERROR,
                f"후보가 실행되지 않았습니다: {(first.error or '')[:120]}",
                {"error": first.error},
            )

        handler = {
            "none": self._observe_only,
            "partition_invariance": self._partition_invariance,
            "range_coverage": self._range_coverage,
            "required_fields_present": self._required_fields_present,
        }.get(probe.invariant)
        if handler is None:
            return (
                ProbeOutcome.UNSUPPORTED,
                f"판정기가 없는 불변식입니다: {probe.invariant}",
                {},
            )
        return handler(observations)

    def _observe_only(
        self, observations: list[RunObservation]
    ) -> tuple[ProbeOutcome, str, dict[str, Any]]:
        first = observations[0]
        return (
            ProbeOutcome.OBSERVED,
            f"{first.record_count}건을 반환했고 위치 "
            f"{_compact(first.requested_positions)} 를 요청했습니다.",
            {"record_count": first.record_count},
        )

    def _partition_invariance(
        self, observations: list[RunObservation]
    ) -> tuple[ProbeOutcome, str, dict[str, Any]]:
        """구간 크기를 바꿔도 같은 집합이 나와야 한다.

        정답을 몰라도 판정할 수 있다는 것이 이 불변식의 핵심이다.
        """
        if len(observations) < 2:
            return ProbeOutcome.UNSUPPORTED, "대조할 실행이 부족합니다.", {}

        sets = [set(o.identity_values) for o in observations]
        if not any(sets):
            # 모든 실행이 0건이면 불변식은 성립하지만 아무것도 확인하지 못했다.
            return (
                ProbeOutcome.VACUOUS,
                "모든 분할에서 0건이라 분할 불변식이 공허하게 성립합니다. "
                "데이터 보존에 대한 증거가 아닙니다.",
                {"per_run": [{"params": o.params, "count": o.record_count}
                             for o in observations]},
            )
        detail = {
            "per_run": [
                {"params": o.params, "count": o.record_count,
                 "ids": sorted(set(o.identity_values))}
                for o in observations
            ]
        }
        if all(s == sets[0] for s in sets):
            return (
                ProbeOutcome.HOLDS,
                f"구간 크기를 바꿔도 같은 {len(sets[0])}건이 나왔습니다.",
                detail,
            )

        union = set().union(*sets)
        missing_by_run = [sorted(union - s) for s in sets]
        detail["union_size"] = len(union)
        detail["missing_by_run"] = missing_by_run
        worst = max(len(m) for m in missing_by_run)
        return (
            ProbeOutcome.VIOLATED,
            f"구간 크기에 따라 결과가 달라집니다. 합집합 {len(union)}건 중 "
            f"최대 {worst}건이 특정 분할에서 빠집니다.",
            detail,
        )

    def _range_coverage(
        self, observations: list[RunObservation]
    ) -> tuple[ProbeOutcome, str, dict[str, Any]]:
        """계약 범위의 모든 위치가 요청에 포함됐어야 한다."""
        expected = set(range(int(self.contract.query["start"]),
                             int(self.contract.query["end"]) + 1))
        first = observations[0]
        requested = set(first.requested_positions)
        gaps = sorted(expected - requested)
        detail = {
            "contract_range": [min(expected), max(expected)],
            "requested": sorted(requested),
            "never_requested": gaps,
        }
        if not gaps:
            return (
                ProbeOutcome.HOLDS,
                f"계약 범위 {min(expected)}~{max(expected)} 를 모두 요청했습니다.",
                detail,
            )
        return (
            ProbeOutcome.VIOLATED,
            f"계약 범위 중 위치 {_compact(tuple(gaps))} 를 한 번도 요청하지 않았습니다.",
            detail,
        )

    def _required_fields_present(
        self, observations: list[RunObservation]
    ) -> tuple[ProbeOutcome, str, dict[str, Any]]:
        """필드명은 계약에 공개돼 있으므로 정답 없이 검사할 수 있다."""
        first = observations[0]
        if not first.record_count:
            return (
                ProbeOutcome.VIOLATED,
                "레코드가 0건이라 필드 보존을 확인할 수 없습니다.",
                {"record_count": first.record_count},
            )
        return (
            ProbeOutcome.HOLDS,
            f"{first.record_count}건에서 식별키가 관측되었습니다.",
            {"record_count": first.record_count,
             "distinct_ids": len(set(first.identity_values))},
        )


def _compact(values: tuple[int, ...]) -> str:
    """연속 구간을 `1~5` 로 줄여 보여준다."""
    if not values:
        return "없음"
    ordered = sorted(values)
    chunks: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        chunks.append(f"{start}~{previous}" if start != previous else str(start))
        start = previous = value
    chunks.append(f"{start}~{previous}" if start != previous else str(start))
    return ",".join(chunks)


def coverage_of(results: list[ProbeResult]) -> set[str]:
    """실제로 실행된 probe 가 덮은 위험 영역.

    `UNSUPPORTED` · `ERROR` 는 실행되지 않았고, `VACUOUS` 는 실행됐지만
    증거를 주지 못했으므로 셋 다 덮은 것으로 세지 않는다.
    """
    covered: set[str] = set()
    for result in results:
        if result.outcome.is_evidence:
            covered.update(result.covers)
    return covered
