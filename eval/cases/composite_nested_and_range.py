"""중첩 경로 오독과 범위 종료 오류가 함께 있다.

분류: composite · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start = query["start"]
    size = query.get("page_size", 2)
    return http.get(f"{BASE}/{start}/{start + size - 1}/").json().get("row", [])
