# gen_public_react 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 프론트엔드 React 템플릿(frontend/react-vite-ts)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java 템플릿은 gen_public 을 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | React 템플릿 3단계 ("3단계 진행해줘") |
| 2026-11-09 | CURRENT v1 → **v2** (요소는 test id 로만 · 응답은 계약 타입 그대로 · 구현마다 다른 요청은 기다리지 않음) | opencode:qwen3.8-flash (머리말 — 알파는 gpt-4.1) | v1 알파 3공고: KB·네이버 review(흔한 구현 3케이스 의도한 사유, 허용 구현 전부 통과), 현대오토에버 failed(계약에 없는 이름 짐작·빈 입력 요청 기다림). 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) | React 템플릿 3단계 ("3단계 진행해줘") |

## v2 (2026-11-09, CURRENT — 위 표)

- 알파 1회 (gpt-4.1, 현대오토에버 공고 명세 재사용, `data/eval/build/2026-11-09-alpha-fe-autoever048-r2`, $0.154): 빌드 **review** —
  흔한 구현 hc1·hc2·hc3 모두 의도한 사유(stale_response_shown·stale_error_shown·cleared_result_reappears)로 실패, 허용 구현 d1 숨은·공개 전부 통과.
  같은 명세의 v1 실행은 failed (계약에 없는 이름 짐작으로 숨은 케이스 전부 예외, 공개 pt3 이 빈 검색어 요청을 기다려 모든 구현 실패).
- 근거 1회 — 같은 실패를 막는 규칙만 더해 해가 될 가능성이 낮다. 다른 공고에서 다시 본다.
