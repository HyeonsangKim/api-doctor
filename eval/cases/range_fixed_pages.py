"""구간 수를 상수로 못 박아 마지막이 빠진다.

분류: range · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    for _ in range(2):
        if cursor > end:
            break
        stop = min(cursor + size - 1, end)
        records.extend(http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]["row"])
        cursor = stop + 1
    return records
