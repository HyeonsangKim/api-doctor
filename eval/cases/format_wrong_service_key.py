"""서비스명을 잘못 적어 중첩 경로가 어긋난다.

분류: format · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        body = http.get(f"{BASE}/{cursor}/{stop}/").json()
        records.extend(body.get("SeoulLibraryInfo", {}).get("row", []))
        cursor = stop + 1
    return records
