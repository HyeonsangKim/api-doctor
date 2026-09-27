"""응답을 파싱하지 않고 문자열로 다룬다.

분류: format · 세트: locked
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    start, end = query["start"], query["end"]
    response = http.get(f"{BASE}/{start}/{end}/")
    if "LBRRY_SEQ_NO" not in response.text:
        return []
    return []
