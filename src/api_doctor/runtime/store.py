"""작업 폴더 · 권한 · 잠금 · 보관 (PRD §4.3, §4.5, AC-11).

관계형 DB 없이 파일로 같은 무결성을 표현한다. 에이전트에게는 이 저장소의
쓰기 권한을 주지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .events import EventStore, RunStatus, atomic_write

RETENTION_DAYS = 7
_RUN_ID_RE = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Resolution(StrEnum):
    OK = "ok"
    NOT_FOUND = "NOT_FOUND"
    EXPIRED = "EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"


def default_runs_root() -> Path:
    override = os.environ.get("API_DOCTOR_HOME")
    base = Path(override) if override else Path.home() / ".api-doctor"
    return base / "runs"


def new_run_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"run_{stamp}_{secrets.token_hex(4)}"


@dataclass(frozen=True, slots=True)
class RunPaths:
    run_dir: Path

    @property
    def manifest(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def events(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def denials(self) -> Path:
        return self.run_dir / "denials.jsonl"

    @property
    def candidates(self) -> Path:
        return self.run_dir / "candidates"

    @property
    def checks(self) -> Path:
        return self.run_dir / "checks"

    @property
    def evidence(self) -> Path:
        return self.run_dir / "evidence"

    @property
    def lock(self) -> Path:
        return self.run_dir / ".lock"


class RunStore:
    """작업 루트 하나를 관리한다."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_runs_root()
        self._swept_this_call: set[str] = set()

    # ------------------------------------------------------------ 생성·해석

    def create(self) -> tuple[str, RunPaths]:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        run_id = new_run_id()
        run_dir = self.root / run_id
        run_dir.mkdir(mode=0o700)
        for sub in ("candidates", "checks", "evidence"):
            (run_dir / sub).mkdir(mode=0o700)
        return run_id, RunPaths(run_dir)

    def resolve(self, run_id: str) -> tuple[Resolution, RunPaths | None]:
        """run_id 를 안전하게 해석한다.

        절대경로·`..`·심볼릭 링크로 루트 밖을 읽지 못하게 한다 (PRD §4.5).
        만료 판정을 위해 호출 시작 시 sweep 을 먼저 돌린다 (AC-11).
        """
        if not _RUN_ID_RE.match(run_id):
            return Resolution.NOT_FOUND, None

        self.sweep()
        if run_id in self._swept_this_call:
            return Resolution.EXPIRED, None

        run_dir = self.root / run_id
        if not run_dir.is_dir():
            return Resolution.NOT_FOUND, None

        # 정규화 후에도 루트 안에 있어야 한다 (심볼릭 링크 탈출 차단).
        try:
            resolved = run_dir.resolve(strict=True)
            resolved.relative_to(self.root.resolve(strict=True))
        except (OSError, ValueError):
            return Resolution.PERMISSION_DENIED, None

        if not os.access(run_dir, os.R_OK | os.X_OK):
            return Resolution.PERMISSION_DENIED, None

        stat = run_dir.stat()
        if stat.st_uid != os.getuid():
            return Resolution.PERMISSION_DENIED, None

        return Resolution.OK, RunPaths(run_dir)

    # ---------------------------------------------------------------- 보관

    def sweep(self, retention_days: int = RETENTION_DAYS) -> set[str]:
        """만료된 작업을 지운다. 모든 CLI 진입점의 첫 단계다.

        이번 호출에서 지운 ID 를 기억해 EXPIRED 와 NOT_FOUND 를 구분한다.
        tombstone 파일은 만들지 않는다 (PRD §4.3).
        """
        if not self.root.is_dir():
            return set()
        cutoff = time.time() - retention_days * 86400
        removed: set[str] = set()
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or not _RUN_ID_RE.match(child.name):
                continue
            try:
                if child.stat().st_mtime >= cutoff:
                    continue
                _remove_tree(child)
                removed.add(child.name)
            except OSError:
                continue
        self._swept_this_call |= removed
        return removed

    def list_runs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        self.sweep()
        return sorted(
            (c.name for c in self.root.iterdir()
             if c.is_dir() and _RUN_ID_RE.match(c.name)),
            reverse=True,
        )

    def delete(self, run_id: str) -> None:
        resolution, paths = self.resolve(run_id)
        if resolution is not Resolution.OK or paths is None:
            raise StoreError(str(resolution), f"{run_id}: {resolution}")
        if is_locked(paths):
            raise StoreError("BUSY", "실행 중인 작업입니다. 먼저 취소하세요.")
        _remove_tree(paths.run_dir)


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


# -------------------------------------------------------------------- 잠금


def acquire_lock(paths: RunPaths) -> None:
    """프로세스 생존 확인형 잠금 (PRD §4.4)."""
    if is_locked(paths):
        raise StoreError("BUSY", "다른 작업이 실행 중입니다.")
    atomic_write(
        paths.lock,
        json.dumps({"pid": os.getpid(), "started": time.time()}),
    )


def is_locked(paths: RunPaths) -> bool:
    if not paths.lock.is_file():
        return False
    try:
        pid = int(json.loads(paths.lock.read_text())["pid"])
    except (OSError, ValueError, KeyError):
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False   # 잠금을 남긴 프로세스가 죽었다
    except PermissionError:
        return True    # 다른 사용자의 살아 있는 프로세스
    return True


def release_lock(paths: RunPaths) -> None:
    paths.lock.unlink(missing_ok=True)


# ------------------------------------------------------------------ manifest


@dataclass(slots=True)
class Manifest:
    """events 에서 파생된 스냅샷. 원자적으로 교체한다."""

    run_id: str
    dataset_id: str
    source: str
    status: str
    stage: str
    created_at: str
    finished_at: str | None = None
    backend_id: str | None = None
    hashes: dict[str, str] = None  # type: ignore[assignment]
    limits: dict[str, Any] = None  # type: ignore[assignment]
    usage: dict[str, Any] = None  # type: ignore[assignment]
    owner_uid: int = 0
    last_seq: int = 0

    def __post_init__(self) -> None:
        self.hashes = self.hashes or {}
        self.limits = self.limits or {}
        self.usage = self.usage or {}

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "source": self.source,
            "status": self.status,
            "stage": self.stage,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "backend_id": self.backend_id,
            "hashes": self.hashes,
            "limits": self.limits,
            "usage": self.usage,
            "owner_uid": self.owner_uid,
            "last_seq": self.last_seq,
        }

    @classmethod
    def load(cls, paths: RunPaths) -> "Manifest":
        data = json.loads(paths.manifest.read_text(encoding="utf-8"))
        return cls(
            run_id=data["run_id"], dataset_id=data["dataset_id"],
            source=data["source"], status=data["status"], stage=data["stage"],
            created_at=data["created_at"], finished_at=data.get("finished_at"),
            backend_id=data.get("backend_id"), hashes=data.get("hashes") or {},
            limits=data.get("limits") or {}, usage=data.get("usage") or {},
            owner_uid=int(data.get("owner_uid", 0)),
            last_seq=int(data.get("last_seq", 0)),
        )

    def save(self, paths: RunPaths, store: EventStore | None = None) -> None:
        if store is not None:
            self.last_seq = store.last_seq
        atomic_write(
            paths.manifest,
            json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n",
        )


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
