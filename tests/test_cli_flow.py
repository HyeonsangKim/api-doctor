"""CLI 전체 흐름 (FR-015, AC-05, AC-11, AC-12)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from api_doctor.cli.main import app
from api_doctor.runtime.events import EventStore, EventType, verify_sequence
from api_doctor.runtime.store import RunStore

from _docker import requires_docker

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("API_DOCTOR_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(ROOT)
    return tmp_path / "home"


@pytest.fixture
def cli():
    return CliRunner()


def _run(cli, *args):
    result = cli.invoke(app, [*args, "--json"])
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result, payload


def test_datasets_lists_registered(cli, home) -> None:
    _, payload = _run(cli, "datasets")
    ids = [d["dataset_id"] for d in payload["datasets"]]
    assert "seoul_library" in ids
    entry = next(d for d in payload["datasets"] if d["dataset_id"] == "seoul_library")
    assert entry["live_ready"] is False, "미검증 live 를 준비됨으로 표기하면 안 된다"
    assert entry["live_blockers"]


@requires_docker
def test_healthy_code_ends_without_model_calls(cli, home) -> None:
    """AC-05: baseline 통과 시 모델과 전문 에이전트를 호출하지 않는다."""
    result, payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_healthy.py")
    )
    assert result.exit_code == 0
    assert payload["status"] == "verified_unchanged"
    assert payload["usage"]["model_calls"] == 0
    assert "candidate.py" not in payload["artifacts"], "정상 코드에 후보를 만들면 안 된다"
    assert "patch.diff" not in payload["artifacts"]


@requires_docker
def test_composite_defect_fails_completeness(cli, home) -> None:
    result, payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_broken.py")
    )
    assert result.exit_code == 3
    assert payload["status"] == "verification_failed"
    checks = {c["kind"]: c["outcome"] for c in payload["verification"]["checks"]}
    assert checks["execution"] == "pass", "실행 자체는 성공한다"
    assert checks["completeness"] == "fail", "데이터는 복구되지 않았다"


@requires_docker
def test_partial_repair_still_fails(cli, home) -> None:
    """수리가 절반만 됐을 때 성공으로 넘어가면 안 된다."""
    _, payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_partial.py")
    )
    assert payload["status"] == "verification_failed"
    checks = {c["kind"]: c["outcome"] for c in payload["verification"]["checks"]}
    assert checks["fields"] == "pass" and checks["values"] == "pass"
    assert checks["completeness"] == "fail"


@requires_docker
def test_show_and_replay_need_no_external_calls(cli, home) -> None:
    """AC-12: 모델키·API키·실행기 없이 기록을 재생한다."""
    _, run_payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_broken.py")
    )
    run_id = run_payload["run_id"]

    _, shown = _run(cli, "show", run_id)
    assert shown["run_id"] == run_id
    assert shown["backend_id"] == "container"

    # 모델·API 키를 지우고 docker 를 PATH 에서 제거해도 재생돼야 한다.
    env = dict(os.environ)
    env.pop("NVIDIA_API_KEY", None)
    env["PATH"] = "/nonexistent"
    result = CliRunner().invoke(app, ["replay", run_id, "--json"], env=env)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["display_mode"] == "replay"
    assert payload["external_calls"] == 0
    assert payload["original_status"] == "verification_failed"


@requires_docker
def test_events_are_append_only_and_sequential(cli, home) -> None:
    _, payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_broken.py")
    )
    run_dir = home / "runs" / payload["run_id"]
    events = EventStore.read(run_dir)
    verify_sequence(events)
    types = [e.type for e in events]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_FINISHED
    assert EventType.CHECK_RESULT in types
    assert sum(t is EventType.MODEL_CALL for t in types) == 0, "M1 은 모델을 부르지 않는다"


@requires_docker
def test_run_directory_permissions(cli, home) -> None:
    """PRD §4.3: 작업 디렉터리 0700, 기록 0600."""
    _, payload = _run(
        cli, "run", "-d", "seoul_library", "-c", str(EXAMPLES / "connector_broken.py")
    )
    run_dir = home / "runs" / payload["run_id"]
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert (run_dir / "manifest.json").stat().st_mode & 0o777 == 0o600
    original = run_dir / "candidates" / "original.py"
    assert original.stat().st_mode & 0o200 == 0, "원본 복사본은 쓰기 불가여야 한다"


def test_unknown_run_is_not_found(cli, home) -> None:
    result, payload = _run(cli, "show", "run_20200101T000000Z_deadbeef")
    assert result.exit_code == 2
    assert payload["error"]["code"] == "NOT_FOUND"


def test_path_traversal_is_refused(cli, home) -> None:
    """PRD §4.5: 절대경로·`..` 로 루트 밖을 읽지 않는다."""
    for evil in ("../../etc/passwd", "/etc/passwd", "run_x/../../etc"):
        result, payload = _run(cli, "show", evil)
        assert result.exit_code == 2
        assert payload["error"]["code"] in {"NOT_FOUND", "PERMISSION_DENIED"}


def test_expired_run_is_distinguished_from_missing(home) -> None:
    """AC-11: 만료는 EXPIRED, 처음부터 없으면 NOT_FOUND."""
    store = RunStore(home / "runs")
    run_id, paths = store.create()
    (paths.run_dir / "manifest.json").write_text("{}")

    old = time.time() - 8 * 86400
    os.utime(paths.run_dir, (old, old))

    from api_doctor.runtime.store import Resolution

    assert store.resolve(run_id)[0] is Resolution.EXPIRED
    assert store.resolve("run_20200101T000000Z_deadbeef")[0] is Resolution.NOT_FOUND
    assert not paths.run_dir.exists(), "만료 기록은 정리되어야 한다"

    # 정리 이후 같은 ID 는 NOT_FOUND 다 (PRD §4.3).
    fresh = RunStore(home / "runs")
    assert fresh.resolve(run_id)[0] is Resolution.NOT_FOUND


def test_unsupported_interface_is_refused(cli, home, tmp_path) -> None:
    bad = tmp_path / "nope.py"
    bad.write_text("def main():\n    pass\n")
    result, payload = _run(cli, "run", "-d", "seoul_library", "-c", str(bad))
    assert result.exit_code == 2
    assert payload["error"]["code"] == "UNSUPPORTED_INPUT"


def test_live_source_refused_while_unverified(cli, home) -> None:
    result, payload = _run(
        cli, "run", "-d", "seoul_library",
        "-c", str(EXAMPLES / "connector_healthy.py"), "--source", "live",
    )
    assert result.exit_code == 2
    assert payload["error"]["code"] == "UNSUPPORTED_INPUT"
    assert "API key" in payload["error"]["message"]


def test_secret_in_code_blocks_before_any_transmission(cli, home, tmp_path) -> None:
    """PRD §4.3: 원문 키가 탐지되면 전송 없이 입력 수정을 안내한다."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text(
        'API_KEY = "a1b2c3d4e5f6g7h8i9j0k1l2"\n\n'
        "def fetch_records(http, query):\n    return []\n"
    )
    result, payload = _run(cli, "run", "-d", "seoul_library", "-c", str(leaky))
    assert result.exit_code == 2
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "전송하지 않았습니다" in payload["error"]["message"]
