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

**결정**: PRD §5.3.4 의 우선 구현안대로 **deepagents 를 채택한다.**
main 은 Deep Agents 의 계획·위임 하네스를 쓰고, 네 전문가는 `subagents` 로
등록된 제한된 agent loop 다.

> ### 정정 기록 (2026-09-27)
>
> 이 문서의 앞선 판에는 "deepagents 를 채택하지 않는다"는 기록이 있었다.
> **그 판정은 틀렸고 철회한다.** 근거 두 가지가 모두 사실이 아니었다.
>
> | 당시 주장 | 실제 |
> |---|---|
> | "`execute`·`write_file` 이 남아 모델이 호스트에 직접 쓸 수 있다" | **기본 backend 가 `StateBackend`** — 에이전트 상태 안의 가상 파일시스템이며 호스트 디스크에 닿지 않는다. `supports_execution(StateBackend)` 는 거짓이라 `execute` 는 애초에 동작하지 않는다 |
> | "제거할 방법이 없다" | `FilesystemMiddleware(tools=[...])` 로 **도구 노드에서 완전히 제거**된다. 스키마에서 숨기는 게 아니라 dispatch 대상에서 빠진다 |
>
> 실패 원인은 `HarnessProfile.excluded_tools` 하나만 시험하고 결론을 낸 것이다.
> 문서에 적힌 `permissions`·`backend`·middleware 경로를 확인하지 않았다.
> 한 가지 방법이 막혔다고 그 기능이 불가능하다고 적어서는 안 된다.

### 경계를 만드는 세 가지 설정 (실측 확인)

**1. `FilesystemMiddleware(tools=["read_file"])`**

기본 9종 중 `execute` · `write_file` · `edit_file` · `delete` · `ls` · `glob` ·
`grep` 가 도구 노드에서 사라진다. `read_file` 은 라이브러리가 필수로 요구해
남기지만 `StateBackend` 상대라 호스트와 무관하다.

**2. `GeneralPurposeSubagentProfile(enabled=False)`**

`task` 도구가 나열하는 위임 대상이 정확히 우리 넷이 된다.

```
task 가 실제로 나열하는 위임 대상: ['spec_researcher', 'runtime_diagnostician',
                                   'repair_engineer', 'data_auditor']
general-purpose 등록됨: False
```

**3. 도구는 전부 `ToolGateway` 가 바인딩한 클로저**

권한은 프롬프트가 아니라 실행 컨텍스트에서 나온다. 모델이 `agent_id` 를
위조해도 무시된다.

### 최종 실측 인벤토리

```
main                    ['get_budget','read_evidence','read_file','request_finish','request_stop','task']
spec_researcher         ['get_budget','read_evidence','search_spec']
runtime_diagnostician   ['get_budget','inspect_code','inspect_trace','read_evidence','run_probe']
repair_engineer         ['get_budget','inspect_code','read_evidence','run_probe','submit_patch']
data_auditor            ['get_budget','inspect_code','inspect_trace','read_evidence','run_probe']

위험 도구 누수: 없음
하위 재위임 가능 역할: 없음
```

### 구현하며 걸린 것들

| 문제 | 원인 | 해결 |
|---|---|---|
| 서브에이전트가 도구를 한 번도 못 부름 | 우리 JSON 프로토콜과 LangChain 의 `tool_calls` 가 이어지지 않음 | `GatewayChatModel._generate` 가 `{"tool":...}` 출력을 `AIMessage.tool_calls` 로 변환 |
| 도구 호출이 전부 `INVALID_ARGS` | 래퍼가 `**kwargs` 시그니처라 LangChain 이 인자 스키마를 만들지 못함 | 도구별 `args_schema` 를 명시 |
| **수리 후 감사가 낡은 후보에서 돌아 `MISSING_AUDIT`** | deepagents 는 서브에이전트를 한 번만 구성하는데 도구 컨텍스트가 그 시점 hash 에 고정됨 | 컨텍스트를 **호출 시점마다** 새로 만들어 현재 `candidate_hash` 를 따라가게 함 |
| `task` 를 감싸 중복 위임을 막으려다 실패 | `task` 는 LangGraph 런타임 주입을 요구해 밖에서 호출할 수 없음 | 네이티브로 돌리고 게이트웨이 호출 기록에서 위임을 복원 |

### 두 하네스를 모두 유지한다

`--harness deepagents` (기본) 와 `--harness builtin` 을 둘 다 제공한다.

| | deepagents (기본) | builtin |
|---|---|---|
| 위임 수단 | Deep Agents 의 `task` | 런타임의 명시적 루프 |
| 위임 대상 제한 | 프레임워크 등록 (4개) | 프로토콜 파서 |
| `NO_NEW_EVIDENCE` 중복 검사 | 없음 (`task` 를 감쌀 수 없음) | **있음** |
| 위임별 lease | 없음 | **있음** |
| 도구 권한·예산·종료 게이트 | 동일 | 동일 |

경계와 판정은 두 경로가 같다. builtin 은 PRD §5.3.3 의 중복 위임 거절을
추가로 강제하므로 평가 비교(§7.3)에 함께 쓴다.

**모델 호출은 두 경우 모두 `GatewayChatModel` 을 지난다.** 프레임워크 내부의
재시도·요약까지 같은 원장에 남는다 (FR-003).
