"""빈 문자열로 덮어써 값 의미가 깨진다.

분류: mapping · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)
    records, cursor = [], start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        for row in http.get(f"{BASE}/{cursor}/{stop}/").json()["SeoulPublicLibraryInfo"]["row"]:
            row = dict(row)
            row["LBRRY_NAME"] = row.get("LBRRY_NAME", "").strip() or " "
            if len(row["LBRRY_NAME"]) > 6:
                row["LBRRY_NAME"] = "   "
            records.append(row)
        cursor = stop + 1
    return records
