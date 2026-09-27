"""정상 연결 코드. baseline 조기 종료(AC-05) 검증용."""


def fetch_records(http, query):
    url = "https://openapi.seoul.go.kr:8088/key/json/SeoulLibraryInfo"
    out, page = [], 1
    while True:
        response = http.get(url, params={"scope_id": query["scope_id"], "page": page, "per_page": 5})
        rows = response.json()["SeoulLibraryInfo"]["row"]
        if not rows:
            break
        out.extend(rows)
        page += 1
    return out
