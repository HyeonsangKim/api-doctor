"""대표 복합 결함 (PRD §3.1).

결함 1 · 중첩 응답 경로 오독: 최상위에서 "row" 를 찾는다.
         실제로는 SeoulPublicLibraryInfo 아래에 있다.
결함 2 · 범위 종료 조건 오류: 첫 구간만 읽고 끝낸다.

실행은 성공하지만 데이터는 복구되지 않는다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start = query["start"]
    size = query.get("page_size", 2)
    response = http.get(f"{BASE}/{start}/{start + size - 1}/")
    return response.json().get("row", [])
