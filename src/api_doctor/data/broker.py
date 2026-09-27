"""Data Broker (FR-004).

Zone S 의 후보가 보내는 요청을 **동결 스냅샷으로만** 해소한다.
후보는 네트워크가 없고, broker 는 스냅샷 밖으로 나가지 않는다.
API key 는 broker 경계 안에만 존재하며 후보·모델·보고서에 전달되지 않는다.

이 API 의 페이지는 query 파라미터가 아니라 **경로의 범위 세그먼트**이고
인증키도 경로에 들어간다 (2026-09-27 실측). 후보는 키 자리에
자리표시자를 쓰고, broker 는 그 세그먼트를 읽지 않는다.
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
    """동결된 공개 응답 모음. 키는 `"{start}:{end}"` 형태다."""

    snapshot_id: str
    dataset_id: str
    service: str
    source_kind: str
    collected_at: str | None
    scope_start: int
    scope_end: int
    scope_record_count: int
    service_total_count: int
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
        scope = data.get("scope") or {}
        # 파일 **원문 바이트**를 해시한다. 재직렬화하면 개행 하나에 hash 가
        # 흔들려 STALE 판정이 거짓이 된다.
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            dataset_id=str(data["dataset_id"]),
            service=str(data.get("service", "")),
            source_kind=str(data.get("source_kind", "unknown")),
            collected_at=data.get("collected_at"),
            scope_start=int(scope.get("start", 1)),
            scope_end=int(scope.get("end", 0)),
            scope_record_count=int(data.get("scope_record_count", 0)),
            service_total_count=int(data.get("service_total_count", 0)),
            pages=dict(data["pages"]),
            snapshot_hash=digest,
        )


@dataclass(slots=True)
class BrokerCall:
    """broker 가 처리한 요청 1건의 기록."""

    url: str
    params: dict[str, Any]
    outcome: str  # served | denied
    reason: str | None = None
    requested_range: tuple[int, int] | None = None
    body: str = ""

    def sanitized(self) -> dict[str, Any]:
        """trace 열람용. 키 자리를 지운 경로만 남긴다."""
        parts = urlsplit(self.url)
        segments = parts.path.strip("/").split("/")
        if segments:
            segments[0] = "{KEY}"
        return {
            "path": "/" + "/".join(segments),
            "outcome": self.outcome,
            "reason": self.reason,
            "requested_range": list(self.requested_range) if self.requested_range else None,
        }


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
            return self._deny(
                url, params, f"데이터 호출 상한 {self.max_calls}회를 초과했습니다."
            )

        endpoint = self._match_endpoint(url)
        if endpoint is None:
            return self._deny(url, params, f"허용되지 않은 endpoint: {url}")

        unknown = set(params) - set(endpoint.allowed_params)
        if unknown:
            return self._deny(url, params, f"허용되지 않은 파라미터: {sorted(unknown)}")

        parsed = self._parse_request(url)
        if parsed is None:
            return self._deny(
                url, params,
                "요청 경로 형태가 올바르지 않습니다. "
                "{KEY}/json/<service>/<start>/<end>/ 형태여야 합니다.",
            )
        service, start, end = parsed
        if service != self.snapshot.service:
            return self._deny(url, params, f"등록되지 않은 서비스: {service}")

        entry = self.snapshot.pages.get(f"{start}:{end}")
        if entry is None:
            # 스냅샷에 없는 변형은 추정하지 않는다 (PRD §3.2 unsupported_probe).
            return self._deny(url, params, f"동결 스냅샷에 없는 범위입니다: {start}~{end}")

        self.calls.append(
            BrokerCall(url, params, "served", requested_range=(start, end),
                       body=entry["body"])
        )
        return {
            "status": 200,
            "headers": {"Content-Type": entry.get("content_type", "application/json")},
            "body": entry["body"],
        }

    # ------------------------------------------------------------------ 검사

    def _match_endpoint(self, url: str):
        parts = urlsplit(url)
        # URL 사용자정보·비표준 스킴을 먼저 막는다 (PRD §4.5).
        if parts.scheme not in ("http", "https") or "@" in parts.netloc:
            return None
        for endpoint in self.dataset.allowed_endpoints:
            if endpoint.method == "GET" and url.startswith(endpoint.url_prefix):
                return endpoint
        return None

    def _parse_request(self, url: str) -> tuple[str, int, int] | None:
        """`.../{KEY}/json/<service>/<start>/<end>/` 를 분해한다.

        키 세그먼트는 **읽지 않는다**. 후보는 자리표시자를 쓰고 실제 키는
        live 경로에서 broker 만 주입한다 (PRD §4.5).
        """
        segments = urlsplit(url).path.strip("/").split("/")
        if len(segments) < 5:
            return None
        _key, fmt, service, start, end = segments[:5]
        if fmt.lower() != "json":
            return None
        try:
            return service, int(start), int(end)
        except ValueError:
            return None

    # ------------------------------------------------------------------ 관측

    def requested_positions(self) -> set[int]:
        """후보가 실제로 요청한 레코드 위치의 합집합. range_coverage 관측에 쓴다."""
        covered: set[int] = set()
        for call in self.calls:
            if call.outcome == "served" and call.requested_range is not None:
                start, end = call.requested_range
                covered |= set(range(start, end + 1))
        return covered

    def served_bodies(self) -> list[str]:
        """실제로 후보에게 전달된 응답 본문. 공급자 응답 분류에 쓴다."""
        return [c.body for c in self.calls if c.outcome == "served" and c.body]

    def trace(self) -> list[dict[str, Any]]:
        """`inspect_trace` 가 반환하는 정제된 요청 기록."""
        return [call.sanitized() for call in self.calls]

    def usage(self) -> dict[str, Any]:
        served = sum(c.outcome == "served" for c in self.calls)
        denied = sum(c.outcome == "denied" for c in self.calls)
        return {
            "total": self.call_count, "served": served,
            "denied": denied, "limit": self.max_calls,
        }
