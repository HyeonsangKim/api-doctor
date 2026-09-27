"""평가 실행기 (FR-017, PRD §7.1~7.3).

두 종류의 평가를 구분한다.

- **검출 평가** (모델 불필요): 각 사례를 baseline 까지 돌려 고정 검증기가
  결함을 옳게 판정하는지 본다. 정상 코드를 훼손하지 않는지, 경계 사례가
  차단되는지도 여기서 확인한다.
- **복구 평가** (모델 필요): 실제로 고쳐지는지. 키가 없으면 실행하지 않고
  "미실행" 으로 표시한다. 하지 않은 것을 했다고 적지 않는다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime.baseline import InputError
from ..runtime.events import RunStatus
from ..runtime.recover import recover
from ..runtime.store import RunStore

# 결함 유형별로 **baseline 단계**에서 나와야 하는 판정.
#
# 최종 status 로 재면 안 된다. 모델 키가 없으면 결함 사례가 전부
# needs_user_action 으로 끝나서, "결함 검출" 이 아니라 "키 없음" 을 재게 된다.
# 검출 정확도는 고정 검증기가 baseline 에서 내린 판정으로만 잰다.
_EXPECT_BASELINE_PASS = {"healthy"}
_EXPECT_POLICY_BLOCK = {"boundary_direct_socket", "boundary_forbidden_endpoint",
                        "boundary_host_file_read"}


@dataclass(slots=True)
class CaseResult:
    name: str
    kind: str
    split: str
    status: RunStatus
    baseline_passed: bool | None
    detected: bool
    denials: int
    policy_denials: int
    model_calls: int
    duration_ms: int
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "split": self.split,
            "status": str(self.status), "baseline_passed": self.baseline_passed,
            "detected": self.detected, "denials": self.denials,
            "policy_denials": self.policy_denials,
            "model_calls": self.model_calls,
            "duration_ms": self.duration_ms, "note": self.note,
        }


@dataclass(slots=True)
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)
    recovery_attempted: bool = False
    recovery_skipped_reason: str = ""

    @property
    def detected(self) -> int:
        return sum(r.detected for r in self.results)

    @property
    def false_positives(self) -> int:
        """정상 코드를 결함으로 오인한 건수. 0이어야 한다."""
        return sum(
            1 for r in self.results
            if r.kind == "healthy" and r.baseline_passed is not True
        )

    @property
    def false_successes(self) -> int:
        """결함 코드를 통과시킨 건수. 0이어야 한다."""
        return sum(
            1 for r in self.results
            if r.kind not in ("healthy", "boundary") and r.baseline_passed is True
        )

    def by_kind(self) -> dict[str, dict[str, int]]:
        rows: dict[str, dict[str, int]] = {}
        for result in self.results:
            row = rows.setdefault(result.kind, {"total": 0, "ok": 0})
            row["total"] += 1
            row["ok"] += int(result.detected)
        return rows

    def to_json(self) -> dict[str, Any]:
        return {
            "cases": [r.to_json() for r in self.results],
            "summary": {
                "total": len(self.results),
                "detection_correct": self.detected,
                "false_positives": self.false_positives,
                "false_successes": self.false_successes,
                "by_kind": self.by_kind(),
                "recovery_attempted": self.recovery_attempted,
                "recovery_skipped_reason": self.recovery_skipped_reason,
            },
        }


def _judge(
    case: dict[str, Any],
    baseline_passed: bool | None,
    policy_denials: int,
    status: RunStatus,
) -> bool:
    """이 사례가 옳게 판정됐는가.

    정상 코드는 통과해야 하고, 결함 사례는 baseline 에서 걸려야 하며,
    경계 사례는 정책 차단 기록이 남아야 한다.
    """
    name, kind = case["name"], case["kind"]

    if kind in _EXPECT_BASELINE_PASS:
        # 정상 코드를 훼손하지 않는다 (거짓 양성 0건).
        return baseline_passed is True and status is RunStatus.VERIFIED_UNCHANGED

    if name in _EXPECT_POLICY_BLOCK:
        # 격리 경계를 건드린 사례는 차단 기록이 남아야 한다.
        return policy_denials > 0 or status is RunStatus.POLICY_BLOCKED

    # 나머지 결함은 고정 검증기가 baseline 에서 실패로 판정해야 한다.
    return baseline_passed is False


def load_cases(root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    return list(manifest["cases"])


def run_detection(
    *,
    cases_root: Path,
    store_root: Path,
    split: str = "all",
    dataset_id: str = "seoul_library",
    on_case: Any = None,
) -> EvalReport:
    """검출 평가. 모델을 호출하지 않는다."""
    report = EvalReport(
        recovery_attempted=False,
        recovery_skipped_reason="검출 평가는 모델을 사용하지 않습니다.",
    )
    store = RunStore(store_root)

    for case in load_cases(cases_root):
        if split != "all" and case["split"] != split:
            continue
        path = cases_root / "cases" / f"{case['name']}.py"
        started = time.monotonic()
        baseline_passed: bool | None = None
        policy_denials = 0
        try:
            outcome = recover(
                dataset_id=dataset_id, code_path=path, store=store, scripted=False,
            )
            status = outcome.status
            baseline = outcome.baseline
            if baseline.verdict is not None:
                baseline_passed = baseline.verdict.passed
            all_denials = list(baseline.denials)
            if baseline.session is not None:
                all_denials += [
                    d for d in baseline.session.denials if d not in baseline.denials
                ]
            denials = len(all_denials)
            policy_denials = sum(d.is_policy_violation for d in all_denials)
            model_calls = int(
                baseline.session.ledger.usage()["model_calls"]
                if baseline.session else 0
            )
            note = ""
        except InputError as exc:
            status = RunStatus.NEEDS_USER_ACTION
            denials, model_calls = 0, 0
            note = f"{exc.code}: {exc}"

        detected = _judge(case, baseline_passed, policy_denials, status)
        result = CaseResult(
            name=case["name"], kind=case["kind"], split=case["split"],
            status=status, baseline_passed=baseline_passed, detected=detected,
            denials=denials, policy_denials=policy_denials,
            model_calls=model_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            note=note,
        )
        report.results.append(result)
        if on_case is not None:
            on_case(result)

    return report
