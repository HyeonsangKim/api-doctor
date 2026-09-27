"""필요한 필드만 골라 담다가 계약 필드를 떨어뜨린다.

분류: mapping · 세트: dev
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
            records.append({"LBRRY_SEQ_NO": row["LBRRY_SEQ_NO"], "LBRRY_NAME": row["LBRRY_NAME"]})
        cursor = stop + 1
    return records
