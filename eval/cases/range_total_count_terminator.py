"""서비스 전체 건수를 조회 범위로 착각해 종료 조건이 틀어진다.

분류: range · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start = query["start"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    while True:
        stop = cursor + size - 1
        body = http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]
        records.extend(body["row"])
        if len(records) >= body["list_total_count"]:
            break
        cursor = stop + 1
    return records
