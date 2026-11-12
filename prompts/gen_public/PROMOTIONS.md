# gen_public 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). Java 템플릿(backend/java-spring-boot3-jpa)이 쓴다 — React·Python 템플릿은 manifest `prompt_tasks` 로 각자의 프롬프트(gen_public_react·gen_public_py)를 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-08 | 첫 버전 CURRENT = **v1** (테스트 먼저 — 공개 수용 기준마다 기능 테스트) | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (기록 파일은 2026-11-10 에 만듦) | 테스트 먼저 사용자 결정 (2026-11-08) |
| 2026-11-10 | CURRENT v1 → **v3** (v3 sha256 2fc47f0cf169…) — 출력 상한(v2·v3) | opencode:qwen3.8-flash (머리말 — 알파는 gpt-4.1) | 전체 알파 2026-11-08 (`2026-11-08-alpha-full-ae023`·`-sg004`, gpt-4.1) 이 plan_contract v11 · gen_public v1 · gen_hidden v3 · gen_starter v7 · gen_naive v4 · gen_accepted v4 를 고정해 두 공고 review — 최신 판은 이 판들에 출력 상한만 바꾼 것(지시문 동일). CURRENT 가 테스트 먼저(v7·v4) 이전 판에 남아 있어 버전 고정 없이 돌린 백엔드 알파(2026-11-09 full2)는 옛 지시문을 썼다. 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) — 같은 승격이 브랜치 chore/generator-current-latest(2026-11-09, 미병합)에 있었다 | 사용자 ("백엔드 생성기 CURRENT 를 최신으로 맞출지 … 이거 진행해주고") |
| 2026-11-12 | CURRENT v3 → **v4** (v4 sha256 e7387f60702b…) — '쓰는 법' 절: 계약·시드가 보장하지 않는 성질은 단언하지 않음 · 꼴 없는 것 통째 비교 금지 · 조작은 빠짐없이 · 헤더는 okHeaders()/HEADER_… (PUBLIC.md 예시의 손 헤더는 모양만) | opencode:qwen3.8-flash (머리말) | N+1 빌드 failed: 공개 테스트가 '모든 주문의 productCount ≥ 1' 을 단언 — 계약·시드는 상품 없는 주문을 막지 않아 허용 구현까지 전부 실패, 재생성 예산 소진 (실제 실패 1건). 백엔드 빌드 16건(qwen, 2026-11-10~) 대조 — 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09). 평가는 다음 백엔드 알파가 첫 평가 | 사용자 (2026-11-12 Java·React 프롬프트 대조 검토 — '권하는 방식: Java 생성기 프롬프트 새 판을 한 번에') |
