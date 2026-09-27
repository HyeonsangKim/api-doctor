"""필드 누락과 마지막 구간 누락이 함께 있다.

분류: composite · 세트: locked
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
        for row in http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]["row"]:
            records.append({k: v for k, v in row.items() if k != "TEL_NO"})
        cursor = stop + 1
    return records
