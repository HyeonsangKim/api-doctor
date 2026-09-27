"""scripted 모델로 1+4 루프를 돌리는 테스트 하네스.

PRD §7.2: scripted 결과와 실제 모델 trace 를 구분해 표기한다.
여기서 확인하는 것은 **구조**이지 모델의 능력이 아니다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from api_doctor.data.broker import Snapshot
from api_doctor.model.gateway import ModelGateway, ScriptedBackend
from api_doctor.registry.loader import load_registry
from api_doctor.runtime.budget import BudgetLedger, Limits
from api_doctor.runtime.evidence import EvidenceStore
from api_doctor.runtime.events import EventStore
from api_doctor.runtime.session import RunSession
from api_doctor.runtime.store import RunStore
from api_doctor.sandbox.base import SandboxLimits
from api_doctor.sandbox.selftest import select_backend
from api_doctor.verify.verifier import load_verifier

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SNAPSHOT_ID = "seoul_library_scope1_5"


def j(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)


def connector(name: str) -> str:
    return (EXAMPLES / f"connector_{name}.py").read_text(encoding="utf-8")


def make_session(tmp_path, start: str = "broken") -> RunSession:
    dataset = load_registry()["seoul_library"]
    snapshot = Snapshot.load(dataset.snapshot_path(SNAPSHOT_ID), dataset.dataset_id)
    run_id, paths = RunStore(tmp_path / "runs").create()
    session = RunSession(
        run_id=run_id, paths=paths, events=EventStore(paths.run_dir),
        ledger=BudgetLedger(limits=Limits(sandbox_runs=12, broker_calls=120)),
        evidence=EvidenceStore(paths.run_dir), dataset=dataset, snapshot=snapshot,
        backend=select_backend(SandboxLimits()).backend,
        verifier=load_verifier(dataset.contract, dataset.expected_path()),
    )
    session.register_candidate(
        source=connector(start), version=0, base_hash=None, author="operator"
    )
    return session


def make_model(session: RunSession, script: list[str]) -> ModelGateway:
    return ModelGateway(
        backend=ScriptedBackend(
            responses=script,
            usage_per_call={"input_tokens": 900, "output_tokens": 180,
                            "total_tokens": 1080},
        ),
        ledger=session.ledger,
    )


def happy_path_script() -> list[str]:
    """main 이 네 역할을 순서대로 쓰되, 모두 근거를 갖고 부르는 경로."""
    return [
        j({"action": "delegate", "agent_id": "spec_researcher",
           "objective": "레코드 배열 경로와 범위 종료 규칙", "reason": "0건이라 구조 확인"}),
        j({"tool": "search_spec", "args": {"question": "레코드 배열 중첩 경로"}}),
        j({"outcome": "completed", "summary": "배열은 SeoulPublicLibraryInfo.row 다."}),

        j({"action": "delegate", "agent_id": "runtime_diagnostician",
           "objective": "계약 범위 전체를 요청하는지 관측", "reason": "실행 지점 확인"}),
        j({"tool": "run_probe", "args": {"probe_id": "range_coverage"}}),
        j({"outcome": "completed", "summary": "위치 1~2 만 요청하고 최상위 row 를 읽는다."}),

        j({"action": "delegate", "agent_id": "repair_engineer",
           "objective": "중첩 경로와 범위 종료를 고친다", "reason": "원인 확인됨"}),
        j({"tool": "submit_patch",
           "args": {"source": connector("healthy"), "rationale": "중첩 경로와 종료 조건"}}),
        j({"outcome": "completed", "summary": "두 결함을 고쳤다."}),

        j({"action": "delegate", "agent_id": "data_auditor",
           "objective": "런타임이 덮어쓴다", "reason": "후보 준비됨"}),
        j({"tool": "run_probe", "args": {"probe_id": "page_partition"}}),
        j({"tool": "run_probe", "args": {"probe_id": "field_presence"}}),
        j({"outcome": "completed", "summary": "손실 근거가 없다.", "findings": [
            {"risk_id": "pagination_boundary", "hypothesis": "마지막 구간 누락",
             "invariant": "partition_invariance", "conclusion": "no_issue",
             "probe_result_ids": ["pr_page_partition"]},
            {"risk_id": "mapping_preservation", "hypothesis": "중첩 경로 오독",
             "invariant": "required_fields_present", "conclusion": "no_issue",
             "probe_result_ids": ["pr_field_presence"]}]}),

        j({"action": "request_finish", "reason": "수리와 독립 감사 완료"}),
    ]
