"""고정 검증기 (FR-006, FR-012, PRD §3.2).

이 모듈은 `agents/` · `tools/` · `model/` 을 import 하지 않는다.
`tests/test_import_boundaries.py` 가 이를 강제한다 — "검증기는 모델과 분리된다"를
문서가 아니라 CI 가 보장하게 만드는 장치다.

네 검사를 **매번 전부** 수행한다. 감사자가 고른 probe 는 추가 조사이며
필수 검사 수를 줄일 수 없다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..registry.loader import Contract


class CheckKind(StrEnum):
    EXECUTION = "execution"      # 실행됐는가
    FIELDS = "fields"            # 필수 필드·타입이 보존됐는가
    VALUES = "values"            # 값 의미가 유지됐는가
    COMPLETENESS = "completeness"  # 전체 결과가 다 왔는가


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class CheckResult:
    kind: CheckKind
    outcome: Outcome
    summary: str
    metrics: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "outcome": str(self.outcome),
            "summary": self.summary,
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """검증 1회의 결과. 이 객체만이 성공을 주장할 수 있다."""

    passed: bool
    candidate_hash: str
    contract_hash: str
    snapshot_hash: str
    results: tuple[CheckResult, ...]

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is not Outcome.PASS)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "candidate_hash": self.candidate_hash,
            "contract_hash": self.contract_hash,
            "snapshot_hash": self.snapshot_hash,
            "checks": [r.to_json() for r in self.results],
        }


@dataclass(frozen=True, slots=True)
class Expectations:
    """비공개 기대값. 검증기만 읽는다."""

    contract_id: str
    contract_version: str
    snapshot_hash: str
    record_count: int
    identity_values: tuple[str, ...]
    records: tuple[dict[str, Any], ...]

    @classmethod
    def load(cls, path: Path, contract: Contract) -> "Expectations":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("contract_id") != contract.contract_id:
            raise ValueError(
                f"기대값의 contract_id 불일치: {data.get('contract_id')}"
            )
        if data.get("contract_version") != contract.version:
            raise ValueError(
                f"기대값의 contract_version 불일치: {data.get('contract_version')}"
            )
        return cls(
            contract_id=str(data["contract_id"]),
            contract_version=str(data["contract_version"]),
            snapshot_hash=str(data["snapshot_hash"]),
            record_count=int(data["record_count"]),
            identity_values=tuple(str(v) for v in data["identity_values"]),
            records=tuple(dict(r) for r in data["records"]),
        )


_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
}


class FixedVerifier:
    """변경 불가능한 기준으로 판정한다.

    모델의 주장·감사자의 찬성·main 의 종료 요청은 입력이 아니다.
    """

    def __init__(self, contract: Contract, expectations: Expectations) -> None:
        self.contract = contract
        self.expectations = expectations

    def verify(
        self,
        *,
        records: list[dict[str, Any]] | None,
        execution_error: str | None,
        candidate_hash: str,
        snapshot_hash: str,
    ) -> Verdict:
        """네 검사를 전부 수행한다. 앞 검사가 실패해도 뒤를 건너뛰지 않는다."""
        if snapshot_hash != self.expectations.snapshot_hash:
            # 기대값이 다른 스냅샷 기준이면 판정할 수 없다 (AC-14).
            inconclusive = CheckResult(
                CheckKind.EXECUTION,
                Outcome.INCONCLUSIVE,
                "기대값과 스냅샷의 hash 가 다릅니다. 이 조합으로는 판정할 수 없습니다.",
                {"expected": self.expectations.snapshot_hash, "actual": snapshot_hash},
            )
            return Verdict(False, candidate_hash, self.contract.contract_hash,
                           snapshot_hash, (inconclusive,))

        results = [
            self._check_execution(records, execution_error),
            self._check_fields(records),
            self._check_values(records),
            self._check_completeness(records),
        ]
        passed = all(r.outcome is Outcome.PASS for r in results)
        return Verdict(
            passed=passed,
            candidate_hash=candidate_hash,
            contract_hash=self.contract.contract_hash,
            snapshot_hash=snapshot_hash,
            results=tuple(results),
        )

    # ------------------------------------------------------------------ 검사

    def _check_execution(
        self, records: list[dict[str, Any]] | None, error: str | None
    ) -> CheckResult:
        if error:
            return CheckResult(
                CheckKind.EXECUTION, Outcome.FAIL,
                f"후보 실행이 실패했습니다: {error[:200]}", {"error": error[:500]},
            )
        if records is None:
            return CheckResult(
                CheckKind.EXECUTION, Outcome.FAIL,
                "후보가 결과를 반환하지 않았습니다.", {},
            )
        return CheckResult(
            CheckKind.EXECUTION, Outcome.PASS,
            f"후보가 {len(records)}건을 반환했습니다.", {"returned": len(records)},
        )

    def _check_fields(self, records: list[dict[str, Any]] | None) -> CheckResult:
        empty = self._empty_verdict(CheckKind.FIELDS, records)
        if empty is not None:
            return empty
        missing: dict[str, int] = {}
        wrong_type: dict[str, int] = {}
        null_violations: dict[str, int] = {}
        for record in records:
            for spec in self.contract.required_fields:
                if spec.name not in record:
                    missing[spec.name] = missing.get(spec.name, 0) + 1
                    continue
                value = record[spec.name]
                if value is None:
                    if not spec.nullable:
                        null_violations[spec.name] = null_violations.get(spec.name, 0) + 1
                    continue
                expected = _TYPES.get(spec.type)
                if expected is not None and not isinstance(value, expected):
                    wrong_type[spec.name] = wrong_type.get(spec.name, 0) + 1

        metrics = {
            "checked": len(records),
            "missing_field_counts": missing,
            "wrong_type_counts": wrong_type,
            "null_violation_counts": null_violations,
        }
        if missing or wrong_type or null_violations:
            parts = []
            if missing:
                parts.append(f"누락 필드 {sorted(missing)}")
            if wrong_type:
                parts.append(f"타입 불일치 {sorted(wrong_type)}")
            if null_violations:
                parts.append(f"null 위반 {sorted(null_violations)}")
            return CheckResult(
                CheckKind.FIELDS, Outcome.FAIL, "; ".join(parts), metrics
            )
        return CheckResult(
            CheckKind.FIELDS, Outcome.PASS,
            f"{len(records)}건에서 필수 필드가 보존되었습니다.", metrics,
        )

    def _check_values(self, records: list[dict[str, Any]] | None) -> CheckResult:
        empty = self._empty_verdict(CheckKind.VALUES, records)
        if empty is not None:
            return empty
        violations: dict[str, int] = {}
        for record in records:
            for rule in self.contract.value_rules:
                value = record.get(rule.field)
                if value is None:
                    continue
                if not self._rule_holds(rule.kind, rule.pattern, value):
                    violations[rule.rule_id] = violations.get(rule.rule_id, 0) + 1
        metrics = {"checked": len(records), "violations": violations}
        if violations:
            return CheckResult(
                CheckKind.VALUES, Outcome.FAIL,
                f"값 규칙 위반: {sorted(violations)}", metrics,
            )
        return CheckResult(
            CheckKind.VALUES, Outcome.PASS,
            f"{len(self.contract.value_rules)}개 값 규칙을 만족합니다.", metrics,
        )

    @staticmethod
    def _rule_holds(kind: str, pattern: str | None, value: Any) -> bool:
        if kind == "regex":
            return bool(pattern) and bool(re.fullmatch(pattern, str(value)))
        if kind == "not_blank":
            return bool(str(value).strip())
        return True  # 알 수 없는 규칙은 통과시키되 등록 검사에서 걸러야 한다

    def _empty_verdict(
        self, kind: CheckKind, records: list[dict[str, Any]] | None
    ) -> CheckResult | None:
        """레코드가 없을 때의 판정. 있으면 None 을 돌려 본검사로 넘긴다.

        데이터가 오지 않았다면 필드·값 보존은 "통과"가 아니라 **확인 불가**다.
        0건을 3/4 초록으로 보여주면 보고서가 거짓말을 한다.
        다만 계약이 애초에 0건을 기대하면 검사할 것이 없는 게 정상이다
        (PRD §3.2 규칙 10).
        """
        if records:
            return None
        if records is None:
            return CheckResult(
                kind, Outcome.FAIL, "후보가 결과를 반환하지 않았습니다.", {"checked": 0}
            )
        if self.expectations.record_count == 0:
            return CheckResult(
                kind, Outcome.PASS,
                "계약이 0건을 기대하며 후보도 0건을 반환했습니다.", {"checked": 0},
            )
        return CheckResult(
            kind, Outcome.INCONCLUSIVE,
            f"레코드가 0건이라 확인할 수 없습니다 (기대 {self.expectations.record_count}건).",
            {"checked": 0, "expected_count": self.expectations.record_count},
        )

    def _check_completeness(self, records: list[dict[str, Any]] | None) -> CheckResult:
        """전체성 검사. 여기가 "실행 성공"과 "데이터 복구"를 가르는 지점이다."""
        if records is None:
            return CheckResult(
                CheckKind.COMPLETENESS, Outcome.FAIL,
                "결과가 없어 전체성을 확인할 수 없습니다.", {},
            )
        key = self.contract.identity_key
        actual = [str(r.get(key)) for r in records if r.get(key) is not None]
        expected = list(self.expectations.identity_values)

        actual_set, expected_set = set(actual), set(expected)
        missing = sorted(expected_set - actual_set, key=_natural)
        unexpected = sorted(actual_set - expected_set, key=_natural)
        duplicates = sorted({v for v in actual if actual.count(v) > 1}, key=_natural)

        metrics = {
            "expected_count": self.expectations.record_count,
            "actual_count": len(records),
            "missing_ids": missing[:20],
            "missing_total": len(missing),
            "unexpected_ids": unexpected[:20],
            "duplicate_ids": duplicates[:20],
        }
        problems = []
        if missing:
            problems.append(f"누락 {len(missing)}건")
        if unexpected:
            problems.append(f"기대 밖 {len(unexpected)}건")
        if duplicates:
            problems.append(f"중복 {len(duplicates)}건")
        if len(records) != self.expectations.record_count:
            problems.append(
                f"건수 {len(records)} != 기대 {self.expectations.record_count}"
            )
        if problems:
            return CheckResult(
                CheckKind.COMPLETENESS, Outcome.FAIL, "; ".join(problems), metrics
            )
        return CheckResult(
            CheckKind.COMPLETENESS, Outcome.PASS,
            f"기대한 {self.expectations.record_count}건이 모두 확인되었습니다.", metrics,
        )


def _natural(value: str) -> tuple[int, str]:
    """ID 를 사람이 읽는 순서로 정렬한다 ("10" 이 "9" 뒤에 오도록)."""
    return (int(value), "") if value.isdigit() else (1 << 30, value)


def load_verifier(contract: Contract, expected_path: Path) -> FixedVerifier:
    return FixedVerifier(contract, Expectations.load(expected_path, contract))
