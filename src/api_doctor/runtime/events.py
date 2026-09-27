"""사건 기록과 manifest (FR-014, 아키텍처 §8.2).

`events.jsonl` 이 **유일한 원천**이다. manifest 는 여기서 파생된 스냅샷이며
원자적 교체로 쓴다. replay 는 events.jsonl 만 읽는다 (AC-12).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


class EventType(StrEnum):
    """아키텍처 §8.2 에서 확정한 사건 타입."""

    RUN_STARTED = "run_started"
    STAGE_CHANGED = "stage_changed"
    BASELINE_RESULT = "baseline_result"
    PLAN_REVISED = "plan_revised"
    DELEGATION_REQUESTED = "delegation_requested"
    DELEGATION_REJECTED = "delegation_rejected"
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_FINISHED = "delegation_finished"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    EVIDENCE_ADDED = "evidence_added"
    PATCH_SUBMITTED = "patch_submitted"
    SANDBOX_RUN = "sandbox_run"
    POLICY_DENIED = "policy_denied"
    CHECK_RESULT = "check_result"
    AUDIT_RETURNED = "audit_returned"
    GATE_EVALUATED = "gate_evaluated"
    BUDGET_EVENT = "budget_event"
    RUN_FINISHED = "run_finished"


class Stage(StrEnum):
    PREPARE = "prepare"
    BASELINE = "baseline"
    DELEGATING = "delegating"
    CHECKING = "checking"
    FINALIZING = "finalizing"


class RunStatus(StrEnum):
    """PRD §3.3 종료 상태."""

    VERIFIED_REPAIRED = "verified_repaired"
    VERIFIED_UNCHANGED = "verified_unchanged"
    NEEDS_USER_ACTION = "needs_user_action"
    EXTERNAL_UNAVAILABLE = "external_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_BLOCKED = "policy_blocked"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def is_success(self) -> bool:
        return self in (RunStatus.VERIFIED_REPAIRED, RunStatus.VERIFIED_UNCHANGED)

    @property
    def exit_code(self) -> int:
        """PRD §5.1.1 종료 코드."""
        if self.is_success:
            return 0
        if self in (RunStatus.BUDGET_EXHAUSTED, RunStatus.POLICY_BLOCKED):
            return 4
        if self is RunStatus.CANCELLED:
            return 130
        if self is RunStatus.INTERRUPTED:
            return 5
        return 3


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    time: str
    type: EventType
    summary: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": self.time,
            "type": str(self.type),
            "summary": self.summary,
            **({"data": self.data} if self.data else {}),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=int(raw["seq"]),
            time=str(raw["time"]),
            type=EventType(raw["type"]),
            summary=str(raw.get("summary", "")),
            data=dict(raw.get("data") or {}),
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """임시 파일에 쓰고 원자적으로 교체한다 (PRD §4.4 RPO)."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


@dataclass(slots=True)
class EventStore:
    """append-only 사건 기록.

    사건 추가 후 즉시 flush+fsync 한다. 비정상 종료 시 마지막 확정 사건까지
    보존되어야 하기 때문이다 (PRD §4.4).
    """

    run_dir: Path
    _seq: int = 0
    _listeners: list[Any] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def last_seq(self) -> int:
        return self._seq

    def subscribe(self, listener: Any) -> None:
        """사건 확정 직후 호출된다. 터미널 진행 출력에 쓴다 (PRD §4.1)."""
        self._listeners.append(listener)

    def append(
        self, type: EventType, summary: str, **data: Any
    ) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, time=_now(), type=type, summary=summary, data=data)
        line = json.dumps(event.to_json(), ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        for listener in self._listeners:
            listener(event)
        return event

    # ------------------------------------------------------------------ 읽기

    @classmethod
    def read(cls, run_dir: Path) -> list[Event]:
        """기록을 순서대로 읽는다. replay 의 유일한 입력이다."""
        path = run_dir / "events.jsonl"
        if not path.is_file():
            return []
        events: list[Event] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(Event.from_json(json.loads(line)))
        return events

    @classmethod
    def iter_read(cls, run_dir: Path) -> Iterator[Event]:
        path = run_dir / "events.jsonl"
        if not path.is_file():
            return
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield Event.from_json(json.loads(line))


class CorruptArtifact(RuntimeError):
    """CORRUPT_ARTIFACT — 기록이 손상되었습니다."""


def verify_sequence(events: list[Event]) -> None:
    """seq 가 1부터 빠짐없이 증가하는지 확인한다."""
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            raise CorruptArtifact(
                f"사건 순번이 어긋납니다: {index}번째 항목의 seq={event.seq}"
            )
