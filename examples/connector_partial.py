"""단일 결함: 중첩 경로는 맞지만 마지막 구간을 놓친다.

수리자가 결함 1 만 고치고 결함 2 를 남긴 중간 상태를 모사한다.
실행·필드·값 검사는 통과하지만 전체성 검사에서 걸린다.

구간 크기에 따라 결과가 달라진다는 점이 중요하다 —
크기 3 이면 두 구간으로 전부 덮이지만, 크기 2 면 마지막 1건이 빠진다.
partition_invariance probe 가 이 어긋남을 기대값 없이 관측한다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"
MAX_PAGES = 2


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    size = query.get("page_size", 2)

    records = []
    cursor = start
    for _ in range(MAX_PAGES):          # 구간 수를 2 로 못 박았다
        if cursor > end:
            break
        stop = min(cursor + size - 1, end)
        response = http.get(f"{BASE}/{cursor}/{stop}/")
        records.extend(response.json()["SeoulPublicLibraryInfo"]["row"])
        cursor = stop + 1
    return records
