"""단일 결함: 중첩 경로는 맞지만 마지막 페이지 경계를 놓친다.

수리자가 결함 1 만 고치고 결함 2 를 남긴 중간 상태를 모사한다.
실행·필드·값 검사는 통과하지만 전체성 검사에서 걸린다.
"""


def fetch_records(http, query):
    url = "https://openapi.seoul.go.kr:8088/key/json/SeoulLibraryInfo"
    out = []
    for page in (1, 2):
        response = http.get(url, params={"scope_id": query["scope_id"], "page": page, "per_page": 5})
        out.extend(response.json()["SeoulLibraryInfo"]["row"])
    return out
