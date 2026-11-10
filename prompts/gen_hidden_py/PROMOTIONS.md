# gen_hidden_py 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 데이터 엔지니어 템플릿(data/python-duckdb)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java·React 템플릿은 각자의 프롬프트를 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | 데이터 엔지니어 진행 ("데이터 엔지니어 일단 추천대로 진행해줘") |
| 2026-11-10 | CURRENT v1 → **v2** | opencode:qwen3.8-flash (머리말) · 알파는 openai:gpt-4.1 | 알파 9회차(plan_contract v19): 테스트는 허용 설계 두 가지(손 구현 포함)에 공정했지만 흔한 구현을 못 잡음 — hc3 이 크래시 뒤 '읽을 수 없음'을 통과로(8회차와 같은 원인: v1 쓰는 법 문장), hc2 가 한 날짜만 비교. → '읽을 수 없음'은 실패 · 출력 전체 비교 · 하루씩 실행. 정적 검사 unreadable_as_pass 와 함께 | 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) |
