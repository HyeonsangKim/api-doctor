"""정상 연결 코드. baseline 조기 종료(AC-05) 검증용.

서울 열린데이터광장의 페이지는 경로의 START/END 범위다.
인증키 자리에는 자리표시자를 쓴다 — 실제 키는 broker 만 안다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)

    records = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + size - 1, end)
        response = http.get(f"{BASE}/{cursor}/{stop}/")
        body = response.json()["SeoulPublicLibraryInfo"]
        records.extend(body["row"])
        cursor = stop + 1
    return records
