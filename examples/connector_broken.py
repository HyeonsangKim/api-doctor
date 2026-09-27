"""대표 복합 결함 (PRD §3.1).

결함 1 · 중첩 응답 경로 오독: 최상위에서 "row" 를 찾는다. 실제로는
         SeoulLibraryInfo 아래에 있다.
결함 2 · 페이지 종료 조건 오류: 첫 페이지만 읽고 끝낸다.

실행은 성공하지만 데이터는 복구되지 않는다 — 이것이 "실행 성공"과
"데이터 복구"를 구분해야 하는 이유다.
"""


def fetch_records(http, query):
    url = "https://openapi.seoul.go.kr:8088/key/json/SeoulLibraryInfo"
    response = http.get(url, params={"scope_id": query["scope_id"], "page": 1, "per_page": 5})
    return response.json().get("row", [])
