# 서울 열린데이터광장 — 공공도서관 현황정보 조사 스킬

> **버전**: v1 · **확인일**: 2026-09-27
> 사람이 검토한 운영 지식이다. 정답·비밀키·숨겨진 테스트를 포함하지 않는다.
> 출처: [이용 안내](https://data.seoul.go.kr/together/guide/useGuide.do),
> [데이터셋 OA-15480](https://data.seoul.go.kr/dataList/OA-15480/A/1/datasetView.do)

## 적용 조건

이 스킬은 `openapi.seoul.go.kr:8088` 의 `SeoulPublicLibraryInfo` 서비스에만 적용한다.
다른 서비스·다른 기관 API 에 이 규칙을 옮겨 쓰지 않는다.

## 요청 형태

```
{scheme}://openapi.seoul.go.kr:8088/{KEY}/json/SeoulPublicLibraryInfo/{start}/{end}/
```

- **페이지가 query 파라미터가 아니다.** 경로의 `{start}`/`{end}` 가 레코드 위치이며
  **1부터 시작하고 양끝을 포함한다.** `?page=` · `?per_page=` 같은 파라미터는 없다.
- 인증키도 경로에 들어간다. 연결 코드는 `{KEY}` 자리표시자를 쓴다.
  실제 키는 broker 만 주입하며 코드·모델·보고서에 노출되지 않는다.
- 한 번에 최대 1,000건까지 요청할 수 있다. 그보다 많은 범위는 나눠 요청한다.

## 응답 구조

성공 응답은 **서비스명으로 한 겹 감싸져 있다.**

```json
{"SeoulPublicLibraryInfo": {
  "list_total_count": 216,
  "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
  "row": [ {...}, ... ]}}
```

- 레코드 배열은 `SeoulPublicLibraryInfo.row` 다. **최상위 `row` 는 존재하지 않는다.**
  최상위에서 `row` 를 찾는 코드는 조용히 빈 배열을 얻는다.
- `list_total_count` 는 **서비스 전체 건수**이지 이번 요청의 건수가 아니다.
  요청 범위의 건수는 `row` 의 길이다. 둘을 혼동하면 종료 조건이 틀어진다.

### 필드

`LBRRY_SEQ_NO`(식별키) · `LBRRY_NAME` · `GU_CODE` · `CODE_VALUE`(자치구) ·
`ADRES` · `TEL_NO` · `HMPG_URL` · `OP_TIME` · `FDRM_CLOSE_DATE` ·
`LBRRY_SE_NAME` · `XCNTS`(위도) · `YDNTS`(경도)

`LBRRY_SEQ_NO` 는 **문자열 형태의 숫자**이며 연속하지 않는다. 순번으로 쓰지 않는다.

## 오류를 구별하는 법

**`/json/` 으로 요청해도 오류는 XML 로 온다.** 2026-09-27 실측.

```xml
<RESULT><CODE>ERROR-335</CODE><MESSAGE><![CDATA[...]]></MESSAGE></RESULT>
```

그래서 `response.json()` 이 던지는 예외가 **파싱 버그가 아니라 서버 측 거절**일 수 있다.
JSON 파싱 실패를 만나면 먼저 본문이 `<RESULT>` 로 시작하는지 확인한다.
`INFO-000` 이 정상이고, `ERROR-3xx` 는 키·범위 문제다.

## 조사 순서

1. 응답 본문의 첫 글자를 본다. `<` 면 XML 오류이므로 코드 수정 이전에 원인을 분류한다.
2. 정상 JSON 이면 `SeoulPublicLibraryInfo.row` 경로가 맞는지 확인한다.
3. 범위를 나눠 가져온다면 **마지막 구간**을 확인한다. `start+size-1` 이 `end` 를
   넘을 때 잘라내지 않으면 스냅샷에 없는 범위를 요청하게 된다.
4. 종료 조건이 `list_total_count` 에 의존하는지 본다. 그 값은 서비스 전체 건수라서
   조회 범위와 다르다.

## 지원하지 않는 조건

- **HTTPS 를 지원하지 않는다** (실측: TLS 핸드셰이크 실패). 키를 전송하는 live 수집은
  이 스킬의 범위 밖이다.
- `sample` 키는 레코드 1~5 만 허용한다. 그 밖의 범위는 `ERROR-335` 다.
- 일일 1,000건 한도가 있다.

## 출처 표시

수집 데이터를 인용할 때 서울 열린데이터광장을 출처로 밝힌다.
원문 재배포 조건은 별도로 확인한다.
