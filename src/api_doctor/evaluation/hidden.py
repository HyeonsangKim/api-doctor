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


@dataclass(slots=True)
class VariantResult:
    name: str
    passed: bool
    returned: int | None
    missing: list[str]
    unexpected: list[str]
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed, "returned": self.returned,
            "missing": self.missing, "unexpected": self.unexpected, "error": self.error,
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
        passed = (
            not missing and not unexpected
            and len(outcome.records) == variant.record_count
        )
        results.append(
            VariantResult(
                name=variant.name, passed=passed, returned=len(outcome.records),
                missing=missing, unexpected=unexpected,
            )
        )

    return HiddenVerdict(passed=all(r.passed for r in results), results=results)
