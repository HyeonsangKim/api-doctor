"""증거 저장소와 가시성 (FR-014, AC-04, 아키텍처 §6).

"독립 감사"는 다른 모델을 쓰는 게 아니라 **입력을 다르게 주는 것**이다.
가시성은 여기서 데이터로 강제되며, 프롬프트로 부탁하지 않는다.

`kind` 의 구분이 핵심이다:
  observation — 도구가 기록한 관측
  assertion   — 모델의 주장
존재하는 근거를 인용해도 주장이 관측으로 승격되지 않는다 (PRD §5.2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

MAIN = "main"
SPEC = "spec_researcher"
DIAG = "runtime_diagnostician"
REPAIR = "repair_engineer"
AUDIT = "data_auditor"
SINGLE = "single_agent"   # 비교 실험 B


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    ASSERTION = "assertion"


class Visibility(StrEnum):
    """누가 이 증거를 읽을 수 있는가."""

    PUBLIC = "public"            # 모든 역할
    SPEC_ONLY = "spec_only"
    RUNTIME_ONLY = "runtime_only"    # 진단·수리·감사 (실행 관측)
    REPAIR_PRIVATE = "repair_private"  # 수리자와 main 만 — 감사자는 못 본다
    MAIN_ONLY = "main_only"
    VERIFIER_ONLY = "verifier_only"  # 어떤 에이전트도 못 본다


# 단일 에이전트(B)는 네 역할의 자료를 모두 본다. 정보 차이로 팀(C)을
# 유리하게 만들지 않기 위해서다 (PRD §7.3). 기대값만은 여전히 못 본다.
_READERS: dict[Visibility, frozenset[str]] = {
    Visibility.PUBLIC: frozenset({MAIN, SPEC, DIAG, REPAIR, AUDIT, SINGLE}),
    Visibility.SPEC_ONLY: frozenset({MAIN, SPEC, SINGLE}),
    Visibility.RUNTIME_ONLY: frozenset({MAIN, DIAG, REPAIR, AUDIT, SINGLE}),
    Visibility.REPAIR_PRIVATE: frozenset({MAIN, REPAIR, SINGLE}),
    Visibility.MAIN_ONLY: frozenset({MAIN}),
    Visibility.VERIFIER_ONLY: frozenset(),
}


def can_read(agent_id: str, visibility: Visibility) -> bool:
    return agent_id in _READERS[visibility]


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: EvidenceKind
    visibility: Visibility
    source: str
    created_by: str
    candidate_hash: str | None
    summary: str
    body: Any
    content_hash: str

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": str(self.kind),
            "visibility": str(self.visibility),
            "source": self.source,
            "created_by": self.created_by,
            "candidate_hash": self.candidate_hash,
            "summary": self.summary,
            "content_hash": self.content_hash,
            "size": len(json.dumps(self.body, ensure_ascii=False)),
        }

    def excerpt(self, max_chars: int = 4000) -> Any:
        """모델에게 전달되는 발췌. 전체 원문을 넘기지 않는다."""
        text = json.dumps(self.body, ensure_ascii=False, indent=2)
        if len(text) <= max_chars:
            return self.body
        return {
            "truncated": True,
            "shown_chars": max_chars,
            "total_chars": len(text),
            "excerpt": text[:max_chars],
        }


@dataclass(slots=True)
class EvidenceStore:
    """run 1개의 증거를 보관한다. 에이전트에게 쓰기 권한은 없다.

    도구 게이트웨이만 `add()` 를 호출하며, 에이전트는 `read()` 로만 접근한다.
    """

    run_dir: Path
    _items: dict[str, Evidence] = field(default_factory=dict)
    _counter: int = 0

    @property
    def dir(self) -> Path:
        return self.run_dir / "evidence"

    def add(
        self,
        *,
        kind: EvidenceKind,
        visibility: Visibility,
        source: str,
        created_by: str,
        summary: str,
        body: Any,
        candidate_hash: str | None = None,
    ) -> Evidence:
        self._counter += 1
        prefix = "obs" if kind is EvidenceKind.OBSERVATION else "asr"
        evidence_id = f"ev_{prefix}_{self._counter:03d}"
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True)
        evidence = Evidence(
            evidence_id=evidence_id,
            kind=kind,
            visibility=visibility,
            source=source,
            created_by=created_by,
            candidate_hash=candidate_hash,
            summary=summary,
            body=body,
            content_hash="sha256:" + hashlib.sha256(serialized.encode()).hexdigest(),
        )
        self._items[evidence_id] = evidence

        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{evidence_id}.json"
        path.write_text(
            json.dumps(
                {**evidence.to_manifest_entry(), "body": body},
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        self._write_manifest()
        return evidence

    def read(
        self, evidence_id: str, *, agent_id: str, candidate_hash: str | None = None
    ) -> Evidence:
        """역할 권한과 hash 신선도를 확인하고 반환한다."""
        evidence = self._items.get(evidence_id)
        if evidence is None:
            raise EvidenceError("NOT_FOUND", f"증거를 찾을 수 없습니다: {evidence_id}")
        if not can_read(agent_id, evidence.visibility):
            raise EvidenceError(
                "FORBIDDEN",
                f"{agent_id} 는 이 증거를 읽을 수 없습니다 ({evidence.visibility}).",
            )
        if (
            candidate_hash is not None
            and evidence.candidate_hash is not None
            and evidence.candidate_hash != candidate_hash
        ):
            raise EvidenceError(
                "STALE",
                f"이전 후보({evidence.candidate_hash[:15]}…)의 증거입니다. "
                "현재 후보의 결과를 다시 수집하세요.",
            )
        return evidence

    def visible_to(self, agent_id: str, candidate_hash: str | None = None) -> list[Evidence]:
        """해당 역할이 볼 수 있는 증거 목록. 목록 자체도 가시성을 따른다."""
        return [
            e for e in self._items.values()
            if can_read(agent_id, e.visibility)
            and (
                candidate_hash is None
                or e.candidate_hash is None
                or e.candidate_hash == candidate_hash
            )
        ]

    def observations_for(self, candidate_hash: str) -> list[Evidence]:
        """특정 후보에 대한 **관측**만. 종료 게이트가 이것만 인정한다."""
        return [
            e for e in self._items.values()
            if e.kind is EvidenceKind.OBSERVATION and e.candidate_hash == candidate_hash
        ]

    def _write_manifest(self) -> None:
        path = self.dir / "manifest.json"
        path.write_text(
            json.dumps(
                [e.to_manifest_entry() for e in self._items.values()],
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
