# plan_contract 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5).
평가: `eval/starter/run_plan_contract.py` — 배송 감사 손 예시(git 밖 `data/feature_starters/delivery-audit/`, r2b)의 지원자 README(상황·요구·수용 기준·제출물 —
**API 계약 절은 넣지 않음**)·진행자 자료로 기능형 명세를 만들고, 레포 재료(sw.backend)의 숨은 요구 t1 + 손 예시 숨은 테스트를 옮긴 숨김 수용 기준 h1·h2 를 붙인다.
나온 계약을 같은 README 의 API 계약 절과 비교한다. 결과는 `data/eval/build/<날짜>-plan-contract-*` (git 밖).

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-07 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 만들 때 CURRENT 로 둠. 스모크 전 | 3단계 구현 (2026-11-07 "3단계 진행해줘") |

CURRENT 가 아닌 버전·평가
- **v1 스모크** (2026-11-07, premium gpt-4.1, 2회, `…-plan-contract-gpt41`): 결정적 검사는 2/2 통과였지만 손 예시와 견주면 쓸 수 없는 계약.
  - 상황·요구에 나온 기존 API(`PATCH /deliveries/{id}/status`)가 빠짐. 공개 요구 r4("기존 API 형식 유지")를 조회 API 에 잘못 연결했고, 요청자 헤더도 조회 API 에 붙임.
  - 자료에 있는 선택 운영 값(연결 대기 5000·스레드 32)을 생략 — 채점 환경이 지원자가 본 조건과 달라진다.
  - 목록 응답 항목 모양을 적을 방법이 없음 (`logs: array`).
  - → 결정적 검사를 보탬 (`starter/contract.py`): 명세·공개 테스트·숨은 케이스에 나온 `METHOD /path` 가 계약에 있어야 함, 자료에서 뽑히는 운영 값은 HiddenPlan 에 있어야 함, 응답 `returns`(엔티티 id·목록 `m1[]`)가 계약 엔티티를 가리켜야 함. 새 검사로 v1 결과를 다시 보면 문제 4건.
  - 비용 $0.034 (2회), 회당 약 7초.
- **v2** — v1 + 기존 API·엔티티도 계약에, 자료의 운영 값은 모두, 응답 `returns`, 요청자 같은 입력은 그 값을 받는 API 에, 테스트·케이스는 계약의 API 만.
  스모크 (2026-11-07, premium gpt-4.1, 2회, `…-plan-contract-gpt41-v2`): 2/2 첫 답에서 통과(새 검사 포함), 두 회차 구조 같음.
  - 손 예시와 같은 것: `PATCH /deliveries/{id}/status`(요청자 헤더 `X-Actor`·본문 `status`·200/404), 배송 건별 감사 기록 조회(목록 `returns: m2[]`),
    감사 필드의 뜻(요청자·시각·이전 상태·요청 상태·결과), HiddenPlan 운영 값 4개 모두 손 예시 하네스 값(풀 8·대기 5000·스레드 32·동시 32),
    숨은 케이스 h1 → `pool_acquire_timeout`(PATCH 32건 동시)·h2 → `rollback_left_success`(저장 단계 오류 주입).
  - 다른 것 (계약이 정하면 되는 선택): 규칙 위반 400(손 예시 409), 조회 경로 `/audit-logs`(손 예시 `/audits`), 필드 이름(`requestedAt`·`previousStatus`·`requestedStatus` ↔ `createdAt`·`fromStatus`·`toStatus`),
    `trackingNo` 없음. 2회차는 요구에 없는 `limit` 쿼리를 더함 — refs(r3)를 달아 결정적 검사로는 못 잡는 작은 범위 넘침.
  - 비용 $0.044 (2회), 회당 약 9초.
  - 승격 대기 — 사용자 확인 필요.
