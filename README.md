# API닥터

깨진 공공 API 연결을 복구하는 에이전트. 메인 에이전트가 네 전문 에이전트에게
조사·진단·수리·감사를 위임하고, **모델과 분리된 고정 검증기**가 데이터가 실제로
복구됐는지 판정한다.

- [PRD v0.2](docs/PRD_api-doctor_v0.2.md) · [아키텍처](docs/ARCHITECTURE_api-doctor_v0.2.md) · [그림](docs/architecture/README.md)
- [Phase 0 기록](docs/PHASE0_keys_and_limits.md) — 키 발급처, 실측 한도, 기술 결정

## 왜 검증기가 따로 있나

연결 코드는 **실행되면서도 데이터를 잃을 수 있다.** 예외가 안 나고 종료 코드가 0이어도
필드가 사라지거나 마지막 구간이 빠지면 사용자에게 필요한 데이터는 복구되지 않는다.

```
$ api-doctor run -d seoul_library -c examples/connector_partial.py

✓  execution     후보가 4건을 반환했습니다.
✓  fields        4건에서 필수 필드가 보존되었습니다.
✓  values        2개 값 규칙을 만족합니다.
✗  completeness  누락 1건; 건수 4 != 기대 5
```

앞의 셋이 전부 통과해도 전체성 검사가 잡아낸다. 이 판정에는 LLM이 관여하지 않는다.

## 에이전트 구조

```
main · recovery_lead          담당자·질문·재계획·종료 요청. 코드를 쓰거나 실행하지 못한다
 ├ spec_researcher            공식 명세의 응답 구조·조회 규칙·오류 구별
 ├ runtime_diagnostician      어디서 왜 실패·손실이 생기는지 관측
 ├ repair_engineer            최소 패치 (이 역할만 코드를 바꾼다)
 └ data_auditor               수리 설명 없이 독립적으로 손실 조사
```

하네스는 **deepagents**가 기본이다 (PRD §5.3.4 우선안). main은 Deep Agents의 `task`로
위임하고, 네 전문가는 `subagents`로 등록된 제한된 agent loop다.

```bash
api-doctor run -d seoul_library -c broken.py                    # deepagents (기본)
api-doctor run -d seoul_library -c broken.py --harness builtin  # 런타임 자체 루프
api-doctor run -d seoul_library -c broken.py --harness single   # 비교 실험 B 대조군
```

기본으로 딸려오는 `execute`(셸)·`write_file`·`delete` 등은 `FilesystemMiddleware(tools=[...])`
로 **도구 노드에서 제거**한다. 남는 `read_file`은 `StateBackend`(에이전트 상태 안의 가상
파일시스템) 상대라 호스트 디스크에 닿지 않는다. general-purpose 하위 에이전트도 끈다.
경계는 매번 컴파일된 그래프를 실측해 확인한다.

### 경계는 프롬프트가 아니라 구조로 만든다

- 감사자는 수리자의 설명도 main의 대화도 받지 않는다. 런타임이 중립 템플릿으로
  envelope를 새로 조립하며, main이 쓴 objective조차 덮어쓴다.
- 권한은 실행 컨텍스트에서 나온다. 모델이 인자로 `agent_id`를 위조해도 무시된다.
- 기대값은 어떤 에이전트도 읽을 수 없다. `verify/`가 `agents/`를 import하면 CI가 실패한다.
- 종료 게이트의 판정 함수는 LLM 출력을 인자로 받지 않는다.

## 현재 상태

| 단계 | 범위 | 상태 |
|---|---|---|
| M0 | 격리 경계 · backend 선택 · preflight | **완료** |
| M1 | registry · broker · 검증기 · 기록 · CLI | **완료** (LLM 없이 완주) |
| M2 | model/tool gateway · 1+4 에이전트 · 종료 게이트 | **완료** |
| M3 | CLI 통합 · 보고서 · 역할별 프로파일 | **완료** |
| M4 | 평가 세트 · B/C 비교 | **검출 평가 완료**, 복구 평가는 키 필요 |

```
테스트 146 passed · 검출 평가 18/18 · 정상 훼손 0건 · 거짓 성공 0건
```

**아직 실제 모델로 복구한 적이 없다.** 구조는 scripted 모델로 검증했고, 보고서는
scripted와 실모델 trace를 **구분해 표기한다**. `NVIDIA_API_KEY`를 넣으면 실모델로 돈다.

## 시작하기

```bash
uv sync --all-extras
uv run api-doctor preflight    # 격리 경계 5종 확인. 모델 불필요
uv run api-doctor datasets
uv run api-doctor eval         # 평가 세트 18건. 모델 불필요

export NVIDIA_API_KEY="nvapi-..."          # build.nvidia.com 무료 발급
uv run api-doctor run -d seoul_library -c examples/connector_broken.py
uv run api-doctor show <run_id>
uv run api-doctor replay <run_id>          # 모델·API·코드 실행 0회
```

`preflight`가 실행 backend를 찾지 못하면 후보 코드를 **호스트에서 대신 실행하지 않는다.**

## 산출물

작업마다 `~/.api-doctor/runs/<run_id>/`에 남는다.

```
events.jsonl              append-only. replay의 유일한 입력
manifest.json             events에서 파생된 스냅샷
candidates/               original.py · v1.py · v2.py
checks/                   3-hash로 키가 잡힌 검사 결과
evidence/                 관측(observation)과 주장(assertion)을 구분해 보관
denials.jsonl             격리 차단 기록 (backend_id 필수)
profiling/                역할별 호출·토큰·지연 + Gantt
report.md · report.json
```

## 제약

- 추가 지출 0원, GPU 없음. 모델은 NVIDIA 무료 hosted endpoint를 다섯 역할이 공유한다.
- 후보 코드는 네트워크가 없는 샌드박스에서만 돈다. 모든 요청은 동결 스냅샷으로 해소된다.
- API key는 broker 경계 안에만 있고 후보·모델·보고서에 전달되지 않는다.
- OpenShell이 우선 backend이지만 Linux 대상이다. macOS에서는 컨테이너로 내려가며,
  **컨테이너 결과를 OpenShell 증거로 표시하지 않는다.**

## 데이터 출처

`registry/datasets/seoul_library`의 스냅샷은 서울 열린데이터광장 `SeoulPublicLibraryInfo`를
공개 `sample` 키로 **실제 호출해 수집**한 것이다. 성공 응답은 실제 수집분을 범위로
자른 것이고, 범위 밖 XML 오류와 인증 오류도 실측분이다. 지어낸 응답은 없다.

실호출로 확인한 것:
- **HTTPS를 지원하지 않는다** (TLS 핸드셰이크 실패) → `live_ready`는 영구히 거짓, fixture 전용
- 페이지가 query 파라미터가 아니라 **경로의 START/END 범위**이고 인증키도 경로에 있다
- **`/json/`으로 요청해도 오류는 XML로 온다** — PRD §1.5가 든 "인증 오류를 파싱 오류로
  오인" 사례가 실재한다

> 출처는 표시하되 원문 재배포 조건은 별도로 확인하지 않았다.

## 개발

```bash
uv run pytest tests/ -q      # docker 없으면 경계 테스트는 자동 skip
```

테스트 스위트는 밀폐돼 있다 — 개발자 환경에 `NVIDIA_API_KEY`가 있어도 실제 API를
호출하지 않는다. `tests/test_import_boundaries.py`가 의존 방향을 AST로 강제한다.
