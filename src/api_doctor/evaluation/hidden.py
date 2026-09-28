"""평가용 비공개 검사 (FR-017, PRD §7.1 b).

제품의 고정 검증과 **분리된** 입력·기대값이다.

- 제품의 고정 검증은 수리 중에 결과를 돌려주며 모델이 기준을 바꿀 수 없다.
- 이 세트는 팀 전체에 숨기고 **최종 패치 확정 후에만** 실행한다.
  평가 실패를 같은 작업의 재수리로 돌려보내지 않는다.

팀은 계약의 주 query(1~5)로 작업하는데 이 세트는 다른 범위를 쓴다.
그 범위에 맞춰 최적화할 방법이 없으므로 "정말 고쳤나"를 독립적으로 잰다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..data.broker import DataBroker, Snapshot
from ..registry.loader import Dataset
from ..sandbox.base import SandboxBackend, SandboxLimits


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    query: dict[str, Any]
    record_count: int
    identity_values: tuple[str, ...]
    #: 이 변형이 후보에게 요청하게 만들어야 하는 구간들 ("2:3" 형식).
    required_ranges: tuple[str, ...] = ()


@dataclass(slots=True)
class VariantResult:
    name: str
    passed: bool
    returned: int | None
    missing: list[str]
    unexpected: list[str]
    #: 계약이 요구하는데 레코드에서 사라진 필드.
    dropped_fields: list[str] = field(default_factory=list)
    #: nullable=false 인데 비어 있는 필드.
    blank_fields: list[str] = field(default_factory=list)
    #: 요청됐어야 하는데 후보가 건드리지 않은 구간.
    unrequested_ranges: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed, "returned": self.returned,
            "missing": self.missing, "unexpected": self.unexpected,
            "dropped_fields": self.dropped_fields,
            "blank_fields": self.blank_fields,
            "unrequested_ranges": self.unrequested_ranges,
            "error": self.error,
        }


@dataclass(slots=True)
class HiddenVerdict:
    """최종 후보에 대한 비공개 판정."""

    passed: bool
    results: list[VariantResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "variants": [r.to_json() for r in self.results],
        }


def load_variants(root: Path) -> list[Variant]:
    data = json.loads((root / "hidden.json").read_text(encoding="utf-8"))
    return [
        Variant(
            name=str(v["name"]), query=dict(v["query"]),
            record_count=int(v["record_count"]),
            identity_values=tuple(str(x) for x in v["identity_values"]),
            required_ranges=tuple(str(x) for x in v.get("required_ranges", ())),
        )
        for v in data["variants"]
    ]


def check(
    *,
    candidate: Path,
    dataset: Dataset,
    snapshot: Snapshot,
    backend: SandboxBackend,
    variants: list[Variant],
    limits: SandboxLimits | None = None,
) -> HiddenVerdict:
    """최종 후보를 비공개 변형들로 실행해 판정한다.

    이 실행은 제품의 예산 원장을 쓰지 않는다 — 평가자의 검사이지
    에이전트의 작업이 아니다.
    """
    limits = limits or SandboxLimits()
    key = dataset.contract.identity_key
    results: list[VariantResult] = []

    for variant in variants:
        broker = DataBroker(dataset=dataset, snapshot=snapshot, max_calls=40)
        outcome = backend.run_candidate(candidate, variant.query, broker.respond, limits)

        if outcome.records is None:
            results.append(
                VariantResult(
                    name=variant.name, passed=False, returned=None,
                    missing=list(variant.identity_values), unexpected=[],
                    error=(outcome.error or "결과 없음")[:200],
                )
            )
            continue

        actual = [str(r.get(key)) for r in outcome.records if r.get(key) is not None]
        expected = set(variant.identity_values)
        missing = sorted(expected - set(actual))
        unexpected = sorted(set(actual) - expected)

        # 건수와 식별키만 보면 필드를 통째로 떨어뜨린 후보가 통과한다.
        # 실제로 그 일이 있었다: mapping 결함이 비공개 검사를 그냥 지나갔고,
        # 그 결과를 "실제로는 고쳐졌다" 로 잘못 읽었다.
        dropped, blank = _field_losses(outcome.records, dataset)

        # 범위를 나눠 가져오는 결함은 결과만 봐서는 안 보일 수 있다.
        # 후보가 어느 위치를 실제로 **요청했는지** 를 브로커가 관측한다.
        #
        # 구간을 글자 그대로 맞추라고 요구하지는 않는다. page_size 를 다르게
        # 써도 정답일 수 있으므로, 필요한 위치를 전부 가져왔는지만 본다.
        unrequested = _unrequested(variant.required_ranges, broker.requested_positions())

        passed = (
            not missing and not unexpected
            and not dropped and not blank and not unrequested
            and len(outcome.records) == variant.record_count
        )
        results.append(
            VariantResult(
                name=variant.name, passed=passed, returned=len(outcome.records),
                missing=missing, unexpected=unexpected,
                dropped_fields=dropped, blank_fields=blank,
                unrequested_ranges=unrequested,
            )
        )

    return HiddenVerdict(passed=all(r.passed for r in results), results=results)


def _unrequested(required_ranges: tuple[str, ...], touched: set[int]) -> list[str]:
    """요청됐어야 하는데 후보가 건드리지 않은 구간."""
    missed: list[str] = []
    for spec in required_ranges:
        try:
            start, end = (int(x) for x in spec.split(":", 1))
        except ValueError:
            continue
        if not set(range(start, end + 1)) <= touched:
            missed.append(spec)
    return missed


def _field_losses(
    records: list[dict[str, Any]], dataset: Dataset
) -> tuple[list[str], list[str]]:
    """계약이 요구하는 필드가 사라졌거나 비었는지 본다.

    `dropped` 는 어느 레코드에도 없는 필드, `blank` 는 nullable=false 인데
    비어 있는 필드다. 둘을 나누는 이유는 원인이 다르기 때문이다 —
    매핑 경로를 잘못 읽으면 필드가 통째로 사라지고, 값을 잘못 꺼내면 빈다.
    """
    dropped: set[str] = set()
    blank: set[str] = set()
    for spec in dataset.contract.required_fields:
        name = spec.name
        if not any(name in r for r in records):
            dropped.add(name)
            continue
        if spec.nullable:
            continue
        for r in records:
            value = r.get(name)
            if value is None or str(value).strip() == "":
                blank.add(name)
                break
    return sorted(dropped), sorted(blank)
