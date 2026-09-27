"""구간 크기가 달라도 전체를 가져오는 정상 코드.

분류: healthy · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = max(1, query.get("page_size", 2) + 1)
    records, cursor = [], start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        records.extend(http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]["row"])
        cursor = stop + 1
    return records
