"""9종 도구의 구현 (PRD §5.1.3).

각 구현의 첫 인자는 런타임이 발급한 `ToolContext` 다. 모델은 이 값을
만들 수도 바꿀 수도 없다. 도구 안에서 역할을 다시 확인하지 않는 이유는
게이트웨이가 이미 바인딩 시점에 걸렀기 때문이며, 대상 제한처럼 역할별로
다른 규칙은 각 구현이 직접 검사한다.
"""

from __future__ import annotations

from typing import Any

from ..registry.loader import Dataset
from ..runtime.budget import BudgetExceeded
from ..runtime.evidence import (
    AUDIT, DIAG, MAIN, REPAIR, EvidenceError, EvidenceKind, Visibility,
)
from ..runtime.events import EventType
from ..runtime.session import RunSession
from ..verify.probes import UnsupportedProbe
from .gateway import Tool, ToolContext, ToolError, ToolGateway

MAX_PATCH_BYTES = 100 * 1024


def register_all(gateway: ToolGateway, session: RunSession) -> None:
    """세션에 묶인 9종 도구를 게이트웨이에 등록한다."""
    gateway.register(Tool.READ_EVIDENCE, _read_evidence(session))
    gateway.register(Tool.SEARCH_SPEC, _search_spec(session))
    gateway.register(Tool.INSPECT_CODE, _inspect_code(session))
    gateway.register(Tool.INSPECT_TRACE, _inspect_trace(session))
    gateway.register(Tool.RUN_PROBE, _run_probe(session))
    gateway.register(Tool.SUBMIT_PATCH, _submit_patch(session))
    gateway.register(Tool.GET_BUDGET, _get_budget(session))
    gateway.register(Tool.REQUEST_FINISH, _request_finish(session))
    gateway.register(Tool.REQUEST_STOP, _request_stop(session))


# ------------------------------------------------------------------ 근거 열람


def _read_evidence(session: RunSession):
    def read_evidence(context: ToolContext, evidence_id: str) -> dict[str, Any]:
        """허용된 근거 1건의 발췌·출처·hash 를 반환한다."""
        try:
            evidence = session.evidence.read(
                evidence_id, agent_id=context.agent_id,
                candidate_hash=context.candidate_hash,
            )
        except EvidenceError as exc:
            raise ToolError(exc.code, str(exc)) from exc
        return {
            "evidence_id": evidence.evidence_id,
            "kind": str(evidence.kind),
            "source": evidence.source,
            "summary": evidence.summary,
            "content_hash": evidence.content_hash,
            "body": evidence.excerpt(),
        }

    return read_evidence


# ------------------------------------------------------------------ 명세 조사


def _search_spec(session: RunSession):
    def search_spec(context: ToolContext, question: str) -> dict[str, Any]:
        """등록된 공식 자료에서 질문에 관련된 절을 찾는다.

        원격 문서를 즉석에서 가져오지 않는다. 사전 검토·버전 고정된
        스킬 자료만 사용한다 (PRD §4.5).
        """
        dataset: Dataset = session.dataset
        skill_path = (
            dataset.root.parents[1] / "skills" / (dataset.skill or "") / "SKILL.md"
        )
        if not dataset.skill or not skill_path.is_file():
            raise ToolError(
                "UNSUPPORTED_DOC",
                f"{dataset.dataset_id} 에 등록된 스킬 자료가 없습니다.",
            )

        sections = _relevant_sections(skill_path.read_text(encoding="utf-8"), question)
        evidence = session.evidence.add(
            kind=EvidenceKind.OBSERVATION, visibility=Visibility.SPEC_ONLY,
            source=f"skill:{dataset.skill}", created_by=context.agent_id,
            summary=f"명세 조회: {question[:80]}",
            body={"question": question, "sections": sections,
                  "skill": dataset.skill, "request_shape": dataset.request_shape},
        )
        session.events.append(
            EventType.EVIDENCE_ADDED, f"명세 근거를 수집했습니다: {evidence.evidence_id}",
            evidence_id=evidence.evidence_id, kind="observation",
            visibility=str(Visibility.SPEC_ONLY), agent_id=context.agent_id,
        )
        return {
            "evidence_id": evidence.evidence_id,
            "sections": sections,
            "request_shape": dataset.request_shape,
        }

    return search_spec


def _relevant_sections(markdown: str, question: str) -> list[dict[str, str]]:
    """제목 단위로 나누고 질문의 낱말이 겹치는 절을 고른다."""
    blocks: list[dict[str, str]] = []
    heading, buffer = "(머리말)", []
    for line in markdown.splitlines():
        if line.startswith("#"):
            if buffer:
                blocks.append({"heading": heading, "text": "\n".join(buffer).strip()})
            heading, buffer = line.lstrip("# ").strip(), []
        else:
            buffer.append(line)
    if buffer:
        blocks.append({"heading": heading, "text": "\n".join(buffer).strip()})

    words = {w for w in question.lower().replace(",", " ").split() if len(w) > 1}
    scored = [
        (sum(w in (b["heading"] + b["text"]).lower() for w in words), b) for b in blocks
    ]
    hits = [b for score, b in sorted(scored, key=lambda x: -x[0]) if score > 0]
    return (hits or [b for _, b in scored])[:4]


# ------------------------------------------------------------------ 코드 열람


def _inspect_code(session: RunSession):
    def inspect_code(context: ToolContext, target: str = "candidate") -> dict[str, Any]:
        """원본 또는 현재 후보의 코드를 읽기 전용으로 반환한다."""
        if target not in ("original", "candidate"):
            raise ToolError("INVALID_ARGS", "target 은 original 또는 candidate 입니다.")

        candidate = (
            session.original if target == "original"
            else session.candidates.get(context.candidate_hash)
        )
        if candidate is None:
            raise ToolError("STALE", "현재 후보를 찾을 수 없습니다.")

        diff = None
        if target == "candidate" and candidate.base_hash:
            diff = _unified_diff(
                session.source_of(candidate.base_hash),
                candidate.path.read_text(encoding="utf-8"),
            )
        return {
            "target": target,
            "candidate_hash": candidate.candidate_hash,
            "version": candidate.version,
            "source": candidate.path.read_text(encoding="utf-8"),
            "diff_from_base": diff,
        }

    return inspect_code


def _unified_diff(before: str, after: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="base", tofile="candidate",
        )
    )


def _inspect_trace(session: RunSession):
    def inspect_trace(context: ToolContext) -> dict[str, Any]:
        """정제된 요청·응답·오류 기록을 반환한다.

        인증키 세그먼트는 `{KEY}` 로 치환돼 있다 (PRD §4.5).
        """
        runs = [
            {"summary": event.summary, **{
                k: v for k, v in event.data.items()
                if k in ("backend_id", "exit_code", "returned", "broker", "query")
            }}
            for event in reversed(session.events.read(session.paths.run_dir))
            if event.type is EventType.SANDBOX_RUN
        ][:6]
        return {
            "sandbox_runs": runs,
            "denials": [d.to_json() for d in session.denials][:10],
            "last_broker_usage": session.last_broker_usage,
        }

    return inspect_trace


# ------------------------------------------------------------------ probe


def _run_probe(session: RunSession):
    def run_probe(context: ToolContext, probe_id: str) -> dict[str, Any]:
        """등록 probe 를 현재 후보에서 실행하고 관측을 반환한다.

        역할별로 대상이 다르다: 감사는 자신에게 허용된 catalog 로만 제한된다.
        """
        if (
            context.agent_id == AUDIT
            and context.allowed_probe_ids
            and probe_id not in context.allowed_probe_ids
        ):
            raise ToolError(
                "UNSUPPORTED_PROBE",
                f"이 작업에 허용된 probe 가 아닙니다: {probe_id}. "
                f"허용: {list(context.allowed_probe_ids)}",
            )

        probe = session.dataset.probes.get(probe_id)
        if probe is None:
            raise ToolError("UNSUPPORTED_PROBE", f"등록되지 않은 probe: {probe_id}")

        verdict = session.ledger.can_run_sandbox()
        if not verdict:
            raise ToolError("BUDGET_EXHAUSTED", verdict.detail)

        try:
            result = session.run_probe(probe_id, context.candidate_hash)
        except UnsupportedProbe as exc:
            raise ToolError("UNSUPPORTED_PROBE", str(exc)) from exc
        except BudgetExceeded as exc:
            raise ToolError("BUDGET_EXHAUSTED", str(exc)) from exc

        evidence = session.evidence.add(
            kind=EvidenceKind.OBSERVATION,
            visibility=Visibility.RUNTIME_ONLY, source=f"probe:{probe_id}",
            created_by=context.agent_id, summary=result.summary,
            body=result.to_json(), candidate_hash=context.candidate_hash,
        )
        session.events.append(
            EventType.EVIDENCE_ADDED,
            f"{probe_id}: {result.summary[:110]}",
            evidence_id=evidence.evidence_id, probe_result_id=result.probe_result_id,
            outcome=str(result.outcome), agent_id=context.agent_id,
            candidate_hash=context.candidate_hash,
        )
        return {
            "probe_result_id": result.probe_result_id,
            "evidence_id": evidence.evidence_id,
            "invariant": result.invariant,
            "outcome": str(result.outcome),
            "summary": result.summary,
            "covers": list(result.covers),
            "detail": result.detail,
        }

    return run_probe


# ------------------------------------------------------------------ 패치


def _submit_patch(session: RunSession):
    def submit_patch(
        context: ToolContext, source: str, rationale: str = ""
    ) -> dict[str, Any]:
        """지정 후보 파일의 새 버전을 만든다. 수리 에이전트만 쓸 수 있다."""
        if len(source.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ToolError("INVALID_PATCH", "후보 파일이 100KB 상한을 넘습니다.")
        if "def fetch_records" not in source:
            raise ToolError(
                "INVALID_PATCH", "fetch_records(http, query) 를 유지해야 합니다."
            )

        verdict = session.ledger.can_submit_patch()
        if not verdict:
            raise ToolError("PATCH_LIMIT", verdict.detail)

        base_hash = context.candidate_hash
        if base_hash not in session.candidates:
            raise ToolError("STALE", "기준 후보를 찾을 수 없습니다.")

        session.ledger.spend_patch()
        candidate = session.register_candidate(
            source=source, version=session.ledger.patches,
            base_hash=base_hash, author=REPAIR,
        )
        # 수리자의 설명은 감사자가 볼 수 없는 가시성으로 저장한다 (R-07).
        if rationale:
            session.evidence.add(
                kind=EvidenceKind.ASSERTION, visibility=Visibility.REPAIR_PRIVATE,
                source="repair_engineer", created_by=context.agent_id,
                summary="패치 근거", body={"rationale": rationale[:2000]},
                candidate_hash=candidate.candidate_hash,
            )
        session.events.append(
            EventType.PATCH_SUBMITTED,
            f"후보 v{candidate.version} 를 만들었습니다.",
            version=candidate.version, base_hash=base_hash,
            candidate_hash=candidate.candidate_hash, agent_id=context.agent_id,
        )
        return {
            "candidate_hash": candidate.candidate_hash,
            "version": candidate.version,
            "base_hash": base_hash,
            "note": "이전 후보에 대한 감사와 검사 결과는 이 시점에 만료됩니다.",
        }

    return submit_patch


# ------------------------------------------------------------------ 예산·종료


def _get_budget(session: RunSession):
    def get_budget(context: ToolContext) -> dict[str, Any]:
        """전체·역할 잔여량을 반환한다."""
        remaining = session.ledger.remaining()
        return {
            "overall": {k: v for k, v in remaining.items() if k != "role_calls"},
            "your_model_calls": remaining["role_calls"].get(context.agent_id, 0),
            "your_lease": context.lease.to_json(),
        }

    return get_budget


def _request_finish(session: RunSession):
    def request_finish(context: ToolContext, reason: str = "") -> dict[str, Any]:
        """고정 게이트를 예약한다. **성공 상태를 입력받지 않는다.**

        main 이 어떤 결론을 주장하든 이 도구는 검증 예약만 한다.
        """
        verdict = session.ledger.reserve_finish_slot()
        if not verdict:
            raise ToolError("BUDGET_EXHAUSTED", verdict.detail)
        session.events.append(
            EventType.DELEGATION_REQUESTED,
            f"main 이 종료 검증을 요청했습니다: {reason[:120]}",
            agent_id=MAIN, candidate_hash=context.candidate_hash, kind="finish",
        )
        return {
            "scheduled": True,
            "candidate_hash": context.candidate_hash,
            "note": "고정 검증과 감사 완료 조건을 통과해야 성공입니다. "
                    "이 요청은 결과를 확정하지 않습니다.",
        }

    return request_finish


_STOP_REASONS = {
    "needs_user_action", "external_unavailable",
    "verification_inconclusive", "budget_exhausted",
}


def _request_stop(session: RunSession):
    def request_stop(
        context: ToolContext, reason: str, evidence_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """비성공 종료만 확정한다. 이미 발생한 강제 중단 사유는 바꿀 수 없다."""
        if reason not in _STOP_REASONS:
            raise ToolError(
                "UNSUPPORTED_REASON",
                f"허용되지 않은 종료 사유: {reason}. 허용: {sorted(_STOP_REASONS)}",
            )
        if session.forced_halt is not None:
            raise ToolError(
                "RUN_TERMINATED",
                f"이미 {session.forced_halt} 로 중단되었습니다. 사유를 바꿀 수 없습니다.",
            )
        session.events.append(
            EventType.DELEGATION_REQUESTED,
            f"main 이 중단을 요청했습니다: {reason}",
            agent_id=MAIN, kind="stop", reason=reason,
            evidence_ids=list(evidence_ids or []),
        )
        return {"accepted": True, "status": reason}

    return request_stop
