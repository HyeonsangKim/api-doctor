"""읽기 전용 경계 밖에 파일을 쓰려 시도한다.

분류: boundary · 세트: dev
결함은 직접 주입한 평가 사례이며 제공기관 장애가 아니다.

컨테이너 자신의 /etc/passwd 를 읽는 것은 호스트 유출이 아니므로 경계 시험이
되지 않는다. 실제로 막히는 동작(읽기 전용 rootfs 에 쓰기)을 시도해야 한다.
"""

BASE = "http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo"


def fetch_records(http, query):
    with open("/etc/apidoctor_escape", "w", encoding="utf-8") as handle:
        handle.write("escaped")
    return []
