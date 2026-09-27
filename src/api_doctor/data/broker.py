"""Data Broker (FR-004).

Zone S 의 후보가 보내는 요청을 **동결 스냅샷으로만** 해소한다.
후보는 네트워크가 없고, broker 는 스냅샷 밖으로 나가지 않는다.
API key 는 broker 경계 안에만 존재하며 후보·모델·보고서에 전달되지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..registry.loader import Dataset


class SnapshotInvalid(RuntimeError):
    """스냅샷 파일이 계약을 만족하지 않습니다."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """동결된 공개 응답 모음."""

    snapshot_id: str
    dataset_id: str
    source_kind: str
    collected_at: str | None
    default_per_page: int
    total: int
    pages: dict[str, Any]
    snapshot_hash: str

    @classmethod
    def load(cls, path: Path, dataset_id: str) -> "Snapshot":
        if not path.is_file():
            raise SnapshotInvalid(f"스냅샷을 찾을 수 없습니다: {path.name}")
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SnapshotInvalid(f"스냅샷 JSON 파싱 실패: {exc}") from exc
        if data.get("dataset_id") != dataset_id:
            raise SnapshotInvalid(
                f"스냅샷의 dataset_id 불일치: {data.get('dataset_id')} != {dataset_id}"
            )
        # 파일 **원문 바이트**를 해시한다. 로드 후 재직렬화하면
        # 들여쓰기·키 순서·개행 하나에 hash 가 흔들려 STALE 판정이 거짓이 된다.
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            dataset_id=str(data["dataset_id"]),
            source_kind=str(data.get("source_kind", "unknown")),
            collected_at=data.get("collected_at"),
            default_per_page=int(data.get("default_per_page", 10)),
            total=int(data["total"]),
            pages=dict(data["pages"]),
            snapshot_hash=digest,
        )


@dataclass(slots=True)
class BrokerCall:
    """broker 가 처리한 요청 1건의 기록."""

    url: str
    params: dict[str, Any]
    outcome: str  # served | denied | not_in_snapshot
    reason: str | None = None


@dataclass(slots=True)
class DataBroker:
    """후보와 collector 가 외부 자료에 닿는 유일한 경로.

    `max_calls` 는 PRD §4.1 의 "공식 데이터 호출 작업 전체 최대 20회" 다.
    동결분 조회도 같은 원장에서 센다.
    """

    dataset: Dataset
    snapshot: Snapshot
    max_calls: int = 20
    calls: list[BrokerCall] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _deny(self, url: str, params: dict[str, Any], reason: str) -> dict[str, Any]:
        self.calls.append(BrokerCall(url, params, "denied", reason))
        return {"denied": True, "reason": reason}

    def respond(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """후보의 요청 1건을 해소한다. Zone S 에서 호출되는 콜백."""
        if self.call_count >= self.max_calls:
            return self._deny(url, params, f"데이터 호출 상한 {self.max_calls}회를 초과했습니다.")

        endpoint = self._match_endpoint(url)
        if endpoint is None:
            return self._deny(url, params, f"허용되지 않은 endpoint: {url}")

        unknown = set(params) - set(endpoint.allowed_params)
        if unknown:
            return self._deny(url, params, f"허용되지 않은 파라미터: {sorted(unknown)}")

        page_key = self._page_key(params)
        body = self.snapshot.pages.get(page_key)
        if body is None:
            # 스냅샷에 없는 변형은 추정하지 않는다 (PRD §3.2 unsupported_probe).
            return self._deny(
                url, params, f"동결 스냅샷에 없는 요청입니다: {page_key}"
            )

        self.calls.append(BrokerCall(url, params, "served"))
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, ensure_ascii=False),
        }

    def _match_endpoint(self, url: str):
        parts = urlsplit(url)
        # URL 사용자정보·비표준 스킴을 먼저 막는다 (PRD §4.5).
        if parts.scheme != "https" or "@" in parts.netloc:
            return None
        for endpoint in self.dataset.allowed_endpoints:
            if endpoint.method == "GET" and url.startswith(endpoint.url_prefix):
                return endpoint
        return None

    def _page_key(self, params: dict[str, Any]) -> str:
        """`per_page:page` 형태의 스냅샷 키로 정규화한다."""
        try:
            per_page = int(params.get("per_page", self.snapshot.default_per_page))
            page = int(params.get("page", 1))
        except (TypeError, ValueError):
            return "invalid"
        return f"{per_page}:{page}"

    def usage(self) -> dict[str, Any]:
        served = sum(c.outcome == "served" for c in self.calls)
        denied = sum(c.outcome == "denied" for c in self.calls)
        return {"total": self.call_count, "served": served, "denied": denied,
                "limit": self.max_calls}
