# gen_naive_react 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 프론트엔드 React 템플릿(frontend/react-vite-ts)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java 템플릿은 gen_naive 을 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | React 템플릿 3단계 ("3단계 진행해줘") |
| 2026-11-10 | CURRENT v1 → **v2** (v2 sha256 c8dcf214ec0f…) — 출력 상한 20000 → 12000 (지시문 동일) | opencode:qwen3.8-flash (머리말 — 알파는 gpt-4.1) | 오늘 알파(gpt-4.1, 낙관적 갱신 명세 samsung#006 재사용, 버전 고정): fe-v3-opt failed(계약 목록 출처 공백 → 계약 검사 추가) · fe-v3-opt-r2 review · fe-v3-main failed(허용 구현 상태 갱신 버그) · fe-v3-main-r2 review — 손 변형 사실 확인으로 숨은 테스트가 함정만 가림. 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) | 프론트 알파 조치 ("지금 멈추고 이거 수정해서 다시 진행해줄래?") |
