"""첫 구간만 읽고 끝낸다.

분류: range · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start = query["start"]
    size = query.get("page_size", 2)
    return http.get(f"{BASE}/{start}/{start + size - 1}/").json()["SeoulPublicLibraryInfo"]["row"]
