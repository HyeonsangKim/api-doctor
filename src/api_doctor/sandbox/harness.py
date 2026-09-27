"""컨테이너 안에서 실행되는 하네스. 표준 라이브러리만 사용한다.

후보는 실제 소켓을 얻지 못한다 (`--network none`). `http.get()`은 stdout 으로
JSONL 요청 1줄을 쓰고 stdin 으로 응답 1줄을 읽는 좁은 IPC 이며, 임의 shell 이나
파일경로 실행을 지원하지 않는다.

이 파일은 Zone S 에서 돌기 때문에 api_doctor 패키지를 import 하지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from typing import Any

MAX_REQUESTS = 64


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class BrokerError(RuntimeError):
    """broker 가 요청을 거절했을 때 후보에게 전달되는 오류."""


class Response:
    """후보에게 노출되는 최소 응답 객체."""

    __slots__ = ("status_code", "headers", "text", "url")

    def __init__(self, url: str, status: int, headers: dict[str, str], text: str) -> None:
        self.url = url
        self.status_code = status
        self.headers = headers
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise BrokerError(f"HTTP {self.status_code} for {self.url}")

    def __repr__(self) -> str:
        return f"<Response {self.status_code} {self.url}>"


class Http:
    """`fetch_records(http, query)` 가 받는 http 객체.

    GET 만 제공한다. 후보가 다른 메서드나 실제 소켓을 시도하면 컨테이너의
    network none 정책에서 실패하며 그 시도는 denial 로 기록된다.
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    @property
    def request_count(self) -> int:
        return self._count

    def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> Response:
        if self._count >= MAX_REQUESTS:
            raise BrokerError("요청 한도를 초과했습니다.")
        self._count += 1
        _emit({"t": "req", "url": url, "params": dict(params or {})})
        line = sys.stdin.readline()
        if not line:
            raise BrokerError("broker 연결이 끊어졌습니다.")
        msg = json.loads(line)
        if msg.get("t") != "res":
            raise BrokerError(f"예상치 못한 broker 메시지: {msg.get('t')!r}")
        if msg.get("denied"):
            raise BrokerError(str(msg.get("reason", "요청이 거절되었습니다.")))
        return Response(
            url=url,
            status=int(msg["status"]),
            headers=dict(msg.get("headers") or {}),
            text=str(msg.get("body", "")),
        )


def _load_candidate(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"후보 모듈을 불러올 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        _emit({"t": "fatal", "error": "usage: harness.py <candidate.py> <query.json>"})
        return 2
    candidate_path, query_path = argv[1], argv[2]
    http = Http()
    try:
        with open(query_path, encoding="utf-8") as fh:
            query = json.load(fh)
        module = _load_candidate(candidate_path)
        fn = getattr(module, "fetch_records", None)
        if not callable(fn):
            _emit({"t": "fatal", "error": "fetch_records 함수를 찾을 수 없습니다."})
            return 2
        records = fn(http, query)
    except BaseException as exc:  # noqa: BLE001 - 후보의 모든 실패를 구조화해 보고한다
        _emit(
            {
                "t": "fatal",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=12),
                "requests": http.request_count,
            }
        )
        return 1

    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        _emit(
            {
                "t": "fatal",
                "error": "fetch_records 는 list[dict] 를 반환해야 합니다.",
                "requests": http.request_count,
            }
        )
        return 2

    _emit({"t": "done", "records": records, "requests": http.request_count})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
