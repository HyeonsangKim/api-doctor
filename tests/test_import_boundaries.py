"""아키텍처 §3.1 의존 방향 규칙을 코드로 강제한다.

"검증기는 모델과 분리되어 있다"를 문서가 아니라 CI 가 보장하게 만드는 테스트다.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "api_doctor"

# (패키지, 금지된 의존 대상, 이유)
FORBIDDEN: list[tuple[str, tuple[str, ...], str]] = [
    ("verify", ("agents", "tools", "model"), "고정 검증기는 모델 판단과 분리된다 (PRD §3.2)"),
    ("model", ("agents", "tools", "verify", "sandbox"), "모델 게이트웨이는 예산 외에 아무것도 모른다"),
    ("sandbox", ("agents", "model", "verify"), "Zone S 는 Zone A 를 모른다"),
]


def _internal_imports(path: Path) -> set[str]:
    """파일이 import 하는 api_doctor 하위 패키지 이름들."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    package = path.relative_to(SRC).parts[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 상대 import: level 1 은 같은 패키지 내부이므로 자기 자신이다.
                found.add(package if node.level == 1 else (node.module or "").split(".")[0])
            elif node.module and node.module.startswith("api_doctor."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("api_doctor."):
                    found.add(alias.name.split(".")[1])
    return found - {package}


def test_dependency_direction() -> None:
    violations: list[str] = []
    for package, banned, reason in FORBIDDEN:
        pkg_dir = SRC / package
        if not pkg_dir.is_dir():
            continue
        for file in sorted(pkg_dir.rglob("*.py")):
            for imported in sorted(_internal_imports(file) & set(banned)):
                violations.append(
                    f"{file.relative_to(SRC)} imports api_doctor.{imported} — {reason}"
                )
    assert not violations, "의존 방향 위반:\n  " + "\n  ".join(violations)


def test_replay_path_has_no_external_dependencies() -> None:
    """AC-12: replay 는 모델·샌드박스·외부 데이터를 import 하지 않는다.

    "외부 호출 0회"를 런타임 플래그가 아니라 의존성 부재로 보장한다.
    """
    replay = SRC / "cli" / "replay.py"
    if not replay.exists():
        import pytest

        pytest.skip("replay 모듈 미구현 (M1)")
    banned = _internal_imports(replay) & {"model", "sandbox", "data", "agents"}
    assert not banned, f"replay 가 외부 의존을 import 합니다: {sorted(banned)}"


def test_harness_is_stdlib_only() -> None:
    """하네스는 Zone S 에서 돌므로 api_doctor 를 import 하면 안 된다."""
    harness = SRC / "sandbox" / "harness.py"
    tree = ast.parse(harness.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    leaked = {n for n in names if n.startswith("api_doctor")}
    assert not leaked, f"하네스가 패키지를 import 합니다: {leaked}"


def test_no_host_backend_exists() -> None:
    """AC-10: 호스트 실행 backend 는 코드베이스에 존재하지 않는다."""
    for file in sorted((SRC / "sandbox").rglob("*.py")):
        source = file.read_text(encoding="utf-8")
        assert "class HostBackend" not in source, f"{file.name} 에 HostBackend 가 있습니다"
