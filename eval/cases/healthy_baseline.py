"""결함이 없는 정상 코드.

분류: healthy · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        records.extend(http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]["row"])
        cursor = stop + 1
    return records
