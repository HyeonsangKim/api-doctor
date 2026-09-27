# API닥터

깨진 공공 API 연결을 복구하는 에이전트. 메인 에이전트가 네 전문 에이전트에게
조사·진단·수리·감사를 위임하고, **모델과 분리된 고정 검증기**가 데이터가 실제로
복구됐는지 판정한다.

- [PRD v0.2](docs/PRD_api-doctor_v0.2.md) — 요구사항
- [아키텍처 v0.2](docs/ARCHITECTURE_api-doctor_v0.2.md) · [그림](docs/architecture/README.md)

## 왜 검증기가 따로 있나

연결 코드는 **실행되면서도 데이터를 잃을 수 있다.** 예외가 안 나고 종료 코드가 0이어도
필드가 사라지거나 마지막 페이지가 빠지면 사용자에게 필요한 데이터는 복구되지 않는다.

```
$ api-doctor run -d seoul_library -c examples/connector_partial.py

✓  execution     후보가 10건을 반환했습니다.
✓  fields        10건에서 필수 필드가 보존되었습니다.
✓  values        2개 값 규칙을 만족합니다.
✗  completeness  누락 2건; 건수 10 != 기대 12

verification_failed
```

앞의 셋이 전부 통과해도 전체성 검사가 잡아낸다. 이 판정에는 LLM 이 관여하지 않는다.

## 현재 상태

| 단계 | 범위 | 상태 |
|---|---|---|
| M0 | 격리 경계 · backend 선택 · preflight | **완료** |
| M1 | registry · broker · 검증기 · 기록 · CLI | **완료** (LLM 없이 완주) |
| M2 | model gateway · tool gateway · 1+4 에이전트 · 종료 게이트 | 미착수 |
| M3 | 복합 결함 복구 데모 · 보고서 · NAT 프로파일러 | 미착수 |
| M4 | 평가 세트 · 단일 에이전트 비교 | 미착수 |

M1 까지는 **모델을 한 번도 호출하지 않는다.** 기준 실행·검증·차단·재생이 전부 모델 없이
동작하는 것이 이 설계의 시금석이다.

## 시작하기

```bash
uv sync --all-extras
uv run api-doctor preflight      # 격리 경계 5종 확인. 모델 불필요
uv run api-doctor datasets
uv run api-doctor run -d seoul_library -c examples/connector_broken.py
uv run api-doctor show <run_id>
uv run api-doctor replay <run_id>   # 모델·API·코드 실행 0회
```

`preflight` 가 실행 backend 를 찾지 못하면 후보 코드를 **호스트에서 대신 실행하지 않는다.**

## 제약

- 추가 지출 0원, GPU 없음. 모델은 NVIDIA 무료 hosted endpoint 를 다섯 역할이 공유한다.
- 후보 코드는 네트워크가 없는 샌드박스에서만 돈다. 모든 요청은 동결 스냅샷으로 해소된다.
- API key 는 broker 경계 안에만 있고 후보·모델·보고서에 전달되지 않는다.
- OpenShell 이 우선 backend 이지만 Linux 대상이다. macOS 에서는 컨테이너로 내려가며,
  **컨테이너 결과를 OpenShell 증거로 표시하지 않는다.**

## 현재 데이터의 출처

`registry/datasets/seoul_library` 의 스냅샷은 **합성 fixture** 다. 공개된 응답 형태를
본떠 직접 작성했으며 실제 API 를 호출해 수집한 것이 아니다 (`source_kind: synthetic_fixture`).
실제 수집으로 교체할 때 `live_ready` 와 `provenance` 를 함께 갱신한다.

## 개발

```bash
uv run pytest tests/ -q          # docker 없으면 경계 테스트는 자동 skip
```

`tests/test_import_boundaries.py` 가 의존 방향을 AST 로 강제한다. `verify/` 가 `agents/`
를 import 하면 CI 가 실패한다 — "검증기는 모델과 분리된다"를 문서가 아니라 코드로 만든다.
