# Phase 0 — 키 발급처와 실측 한도

> **확인일**: 2026-09-27 · Asia/Seoul
> 아래는 **실제로 확인한 것**만 적는다. 추정은 "미확인"으로 표시한다.

## 1. NVIDIA — 모델 추론 키

| 항목 | 내용 |
|---|---|
| **발급처** | <https://build.nvidia.com> |
| 절차 | 무료 계정 생성 → 이메일(경우에 따라 전화) 인증 → 프로필에서 API key 생성 |
| 키 형식 | `nvapi-…` |
| 비용 | 무료. 신용카드 불필요 |
| **무료 크레딧** | 가입 시 **1,000 inference credits**. 프로필 → *Request More* 로 최대 5,000까지 요청 가능 |
| 추가 크레딧 | 개인 이메일로 가입했다면 **회사 이메일** 등록 시 90일 NVIDIA AI Enterprise 라이선스와 함께 4,000 추가 |
| **요청 한도** | 분당 40 요청 |
| Base URL | `https://integrate.api.nvidia.com/v1` (OpenAI 호환) |
| 모델 ID | `nvidia/nemotron-3-super-120b-a12b` |
| 환경변수 | `NVIDIA_API_KEY` |

```bash
export NVIDIA_API_KEY="nvapi-..."
uv run api-doctor preflight     # 키 인식 여부 확인
```

### 이 한도가 예산 설계에 미치는 영향

PRD §4.1 은 작업 1건당 모델 호출 최대 24회를 상한으로 둔다. 실측된 원장으로는
**19회에서 토큰이 먼저 고갈**된다 ([아키텍처 §5.1](ARCHITECTURE_api-doctor_v0.2.md)).
크레딧 1,000 을 호출 1회 = 1 크레딧으로 보수적으로 잡으면 **전체 작업 약 50건**이
무료 범위다. 평가 세트 26건 + 개발 반복을 고려하면 여유가 크지 않으므로,
PRD §4.1 의 "공급자의 무료 한도가 더 낮으면 항상 낮은 한도를 적용한다"에 따라
실제 크레딧 소모율을 첫 호출에서 측정해 상한을 다시 고정한다.

> 크레딧 1건이 호출 1회인지 토큰 기준인지는 **미확인**이다. 첫 실호출에서 확인한다.

## 2. 서울 열린데이터광장 — 공공데이터 키

| 항목 | 내용 |
|---|---|
| **발급처** | <https://data.seoul.go.kr> → 인증키 신청 |
| 이용 안내 | <https://data.seoul.go.kr/together/guide/useGuide.do> |
| 비용 | 무료 |
| **일일 한도** | 1,000 건/일 |
| 1회 최대 | 1,000 건 |
| 대상 데이터셋 | [서울시 공공도서관 현황정보 (OA-15480)](https://data.seoul.go.kr/dataList/OA-15480/A/1/datasetView.do) |

### 요청 형태 (실측)

```
http://openapi.seoul.go.kr:8088/{인증키}/json/SeoulPublicLibraryInfo/{시작}/{끝}/
```

- 페이지가 query 파라미터가 아니라 **경로의 범위 세그먼트**다. 1-based, 양끝 포함.
- **인증키가 URL 경로에 들어간다.**
- `sample` 키로 인증 없이 시험할 수 있으나 **1~5행만** 허용된다.

### 실측으로 확인한 3가지 — 전부 설계에 반영했다

**① HTTPS 를 지원하지 않는다**

```
https → HTTP 000 · TLS 핸드셰이크 실패
http  → HTTP 200
```

PRD §4.5 는 "HTTPS 미확인 데이터 소스는 key 를 전송하는 live 지원에서 제외하고
출처가 확인된 키 없는 fixture 만 사용한다"고 정했다. 따라서 이 endpoint 의
`live_ready` 는 **앞으로도 거짓**이며 fixture 전용이다. 레지스트리 로더는
`live_ready: true` + 비-HTTPS 조합을 거절한다.

**② 인증키가 URL 경로에 있다**

후보 코드가 URL 을 직접 만들면 키가 후보 안으로 들어온다. 그래서 후보는
`{KEY}` 자리표시자를 쓰고 broker 가 그 세그먼트를 **읽지 않는다**.
`inspect_trace` 가 반환하는 기록에서도 첫 세그먼트는 `{KEY}` 로 치환된다.

**③ `/json/` 으로 요청해도 오류는 XML 로 온다**

```
GET /sample/json/SeoulPublicLibraryInfo/6/10/
<RESULT><CODE>ERROR-335</CODE><MESSAGE><![CDATA[샘플데이터(샘플키:sample) 는
한번에 최대 5건을 넘을 수 없습니다...]]></MESSAGE></RESULT>
```

PRD §1.5 가 든 "인증 오류도 XML 로 온다 → 파싱 오류로 오인" 사례가 실제로 존재한다.
이 XML 오류 응답을 동결 스냅샷에 포함해, 진단 에이전트가 실제로 마주치게 했다.

## 3. 현재 fixture 의 출처

`registry/datasets/seoul_library/snapshots/seoul_library_scope1_5.json`

| 항목 | 값 |
|---|---|
| source_kind | `live_capture` |
| 수집 방법 | `sample` 키로 실제 호출 (공개 데이터) |
| 수집 시각 | 2026-09-27 |
| 계약 조회 범위 | 레코드 1~5 (5건) |
| 서비스 전체 | 216건 |
| 필드 | 실제 응답의 12개 필드 중 6개를 계약이 요구 |

성공 응답은 실제 수집분을 범위로 자른 것이고, 범위 밖 XML 오류도 실제 관측분이다.
**지어낸 응답은 없다.** 서비스 전체 216건 수집은 발급 키가 있어야 한다.

> 출처 표시는 하되 **원문 재배포 조건은 별도로 확인하지 않았다.**

## 4. 아직 확인하지 않은 것

| 항목 | 왜 필요한가 |
|---|---|
| 해커톤 Skill API 요건·마감·제출 형식 | PRD §0.3. 자체 SKILL.md 로 충족되는지 불명 |
| Nemotron 의 tool roundtrip·구조화 반환·reasoning 상한 | 실호출 전까지 미확정 |
| 크레딧 1건의 단위 (호출 / 토큰) | 예산 상한 재고정에 필요 |
| KOSIS 키 조건 | 두 번째 데이터셋 후보 (Phase 3) |

## 5. 기술 결정 기록 — main 의 하네스

**결정**: deepagents 를 main 의 하네스로 **채택하지 않는다.** PRD §5.3.4 가 명시한
대체안(동일 1+4 구조·도구 계약·LangGraph 상태를 유지하는 supervisor)을 쓴다.

**근거 (deepagents 0.7.19, 2026-09-27 실측)**

기본으로 9개 도구를 싣는다. `tools=[]` · `subagents=[]` 로도 제거되지 않는다.

```
기본값        ['delete','edit_file','execute','glob','grep','ls','read_file','task','write_file']
tools=[]      (동일)
subagents=[]  (동일)
```

`HarnessProfile(excluded_tools=[...])` 로 일부는 제거된다. 그러나 프로파일 키가
**모델 spec/provider 에 묶여** 있고, 제거 결과는 다음과 같다.

```
제한 후       ['delete','edit_file','execute','get_budget','glob','grep','ls','read_file','write_file']
제거된 것     task  (동적 위임 — 이것만 빠진다)
남은 것       execute(셸) · write_file · delete · 파일시스템 전체
```

`execute` 와 파일시스템 도구는 `FilesystemMiddleware` 소속이고, 이 미들웨어는
라이브러리의 **보호된 scaffolding** 이라 `excluded_middleware` 로 제외하면
`ValueError` 가 난다.

**이것이 왜 차단 사유인가**

- AC-01 은 "등록된 위임 대상은 4개뿐이고 기본 general-purpose·동적 생성·하위
  재위임을 사용할 수 없다"를 요구한다. `task` 는 제거되므로 이 부분은 만족한다.
- 그러나 AC-07 은 "명세 조사자가 파일 변경을 요청하면 프롬프트와 무관한 실행
  컨텍스트 권한으로 차단한다"를 요구한다. `execute` 와 `write_file` 이 남아 있으면
  **모델이 도구 게이트웨이를 우회해 호스트에 직접 쓸 수 있다.** 이는 Zone T 와
  Zone A 의 경계 자체를 무너뜨린다.
- PRD §5.3.4 가 요구하는 "초기화 후 실제 tool inventory 검사"를 통과할 수 없다.

**대신 채택한 것**

main 과 네 전문가 모두 `model/gateway.py` 위의 제한된 agent loop 로 구현한다.
이 경로의 이점은 부수적이 아니라 요구사항 직결이다 — FR-003 은 "모든 SDK 요청·
자동 재시도·구조화 출력 재시도·요약·보조 모델은 동일 gateway 를 통과한다"를
요구하는데, 외부 에이전트 프레임워크를 쓰면 그 내부 호출을 원장에 싣는 것을
보장하기 어렵다. 직접 구현하면 **모델 호출 경로가 하나뿐**임이 자명하다.

LangGraph 는 stage 상태기계와 중단 처리에 계속 쓴다. 1+4 구조·도구 계약·역할별
가시성은 그대로다. **단일 에이전트로 축소하지 않는다.**

> 이 판정은 deepagents 0.7.19 기준이다. 상위 버전에서 `FilesystemMiddleware` 를
> 끌 수 있게 되면 재검토한다.
