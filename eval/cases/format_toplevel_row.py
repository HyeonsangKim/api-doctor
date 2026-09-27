"""중첩 경로를 무시하고 최상위에서 row 를 찾는다.

분류: format · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        records.extend(http.get(f"{BASE}/{cursor}/{stop}/").json().get("row", []))
        cursor = stop + 1
    return records
