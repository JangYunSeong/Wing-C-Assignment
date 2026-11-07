# gen_starter 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5).
평가: `eval/starter/run_generate.py` — 배송 감사 손 예시 명세 + plan_contract v3 계약(`data/eval/build/2026-11-07-plan-contract-gpt41-v3c/contract-1.json`, git 밖).
`--starter-only N` 은 시작 코드만 생성·개발 서버 Gradle 빌드, `--loop` 는 수리 루프 전체.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-07 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 만들 때 CURRENT 로 둠 (5단계) | 5단계 구현 ("1,2 수행하고 5단계 진행해줘") |

## v2 (2026-11-07, 아직 CURRENT 아님)

v1 의 시작 코드 자체 테스트가 **4회 중 3회** 실패 (생성 스모크 r2·tables, 수리 루프 1회 — 수리 요청으로 다시 만든 것도 같음): 기존 기능 테스트 두 개가 같은 시드 행(id 1)을 바꿔
실행 순서에 따라 "READY→DELIVERED 는 400" 확인이 200 이 됨. v2 = v1 + "테스트 메서드마다 서로 다른 시드 행, 처음 상태는 시드 값만 가정, 작은 id 만" 규칙.

- `--starter-only 2` (gpt-4.1, `…-starter-v2`, $0.083): 2/2 시작 코드 테스트 통과, 테스트마다 다른 행 (1·2·999 / 1·22·999).
  22 는 지원자 몫(id 1~10) 밖 — 숨은 케이스 범위와 겹칠 수 있어 계약 시드 줄에 "테스트에서 바꿔도 되는 행은 id 1~10" 을 적게 함 (seed_rows.seed_markdown).
- 수리 루프 1회 (`--loop --pin gen_starter=2`, `…-loop-starter-v2`, $0.175): 시작 코드 통과. 숨은 테스트가 **처음으로 의도대로 가름** — 흔한 구현은 hc1·hc2 모두 의도한 사유
  (`pool_acquire_timeout` · `rollback_left_success`)로 실패, 허용 구현 d1 은 둘 다 통과. 남은 실패는 시작 코드와 무관 (두 참조 구현이 '없음 404' 기존 테스트에서 not-null — 계약 모호, spec-to-code §7).
- 시작 코드 테스트 통과: v1 1/4 → v2 3/3.
