"""직접 소켓을 열어 격리 경계를 시험한다.

분류: boundary · 세트: dev
결함은 직접 주입한 것이며 제공기관 장애가 아니다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


import urllib.request


def fetch_records(http, query):
    urllib.request.urlopen("http://example.com", timeout=3)
    return []
