# gen_starter 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5).
평가: `eval/starter/run_generate.py` — 배송 감사 손 예시 명세 + plan_contract v3 계약(`data/eval/build/2026-11-07-plan-contract-gpt41-v3c/contract-1.json`, git 밖).
`--starter-only N` 은 시작 코드만 생성·개발 서버 Gradle 빌드, `--loop` 는 수리 루프 전체.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-07 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 만들 때 CURRENT 로 둠 (5단계) | 5단계 구현 ("1,2 수행하고 5단계 진행해줘") |
| 2026-11-07 | CURRENT v1 → **v2** (기존 기능 테스트는 테스트 메서드마다 서로 다른 시드 행) | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 아래 v2 절: 시작 코드 자체 테스트 통과 v1 1/4 → v2 3/3, 수리 루프에서 숨은 테스트가 의도대로 가름. 비용 $0.26 | 사용자 확인 (2026-11-07 "1번 진행해주고" — v2 승격 제안에) |
| 2026-11-07 | CURRENT v2 → **v3** (수리면 자기 이전 출력을 받아 고칠 파일만) | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 아래 v3 절: 배송 감사 루프에서 수리 2건 모두 고칠 파일만 바뀌고 그 한 번에 해결 (근거 1회 — 해가 될 가능성이 낮아 승격). 비용 $0.18 | 사용자 확인 (2026-11-07 "1,2,3 진행하고" — 수리판 승격 제안에) |

## v2 (2026-11-07, CURRENT — 위 표)

v1 의 시작 코드 자체 테스트가 **4회 중 3회** 실패 (생성 스모크 r2·tables, 수리 루프 1회 — 수리 요청으로 다시 만든 것도 같음): 기존 기능 테스트 두 개가 같은 시드 행(id 1)을 바꿔
실행 순서에 따라 "READY→DELIVERED 는 400" 확인이 200 이 됨. v2 = v1 + "테스트 메서드마다 서로 다른 시드 행, 처음 상태는 시드 값만 가정, 작은 id 만" 규칙.

- `--starter-only 2` (gpt-4.1, `…-starter-v2`, $0.083): 2/2 시작 코드 테스트 통과, 테스트마다 다른 행 (1·2·999 / 1·22·999).
  22 는 지원자 몫(id 1~10) 밖 — 숨은 케이스 범위와 겹칠 수 있어 계약 시드 줄에 "테스트에서 바꿔도 되는 행은 id 1~10" 을 적게 함 (seed_rows.seed_markdown).
- 수리 루프 1회 (`--loop --pin gen_starter=2`, `…-loop-starter-v2`, $0.175): 시작 코드 통과. 숨은 테스트가 **처음으로 의도대로 가름** — 흔한 구현은 hc1·hc2 모두 의도한 사유
  (`pool_acquire_timeout` · `rollback_left_success`)로 실패, 허용 구현 d1 은 둘 다 통과. 남은 실패는 시작 코드와 무관 (두 참조 구현이 '없음 404' 기존 테스트에서 not-null — 계약 모호, spec-to-code §7).
- 시작 코드 테스트 통과: v1 1/4 → v2 3/3.

## v3 (2026-11-07, CURRENT — 위 표)

v2 + 수리면 자기 이전 출력을 받아 고칠 파일만 출력 (gen_hidden·gen_naive·gen_accepted v2 와 같은 변경).

- 수리 루프 1회 (gpt-4.1, 배송 감사 v5 계약 run 1, gen_starter v3·gen_hidden v2·gen_naive v2·gen_accepted v2 고정, `data/eval/build/2026-11-07-loop-repair-prev`, $0.18): 수리 2건 모두 **고칠 파일만** 바뀜 — 숨은 테스트 3파일 중 1개(테스트 자체 예외가 난 hc2), d1 2파일 중 1개(기존 테스트를 깬 서비스). 두 문제 모두 그 한 번에 고쳐짐. 남은 실패는 다시 만든 hc2 의 주입이 너무 넓음(`failCommitAfter = "delivery|delivery_audit_log"` — 실패 기록 INSERT 의 커밋까지 막아 어떤 설계도 통과 못 함) → 루프가 '숨은 테스트·계약 의심'으로 사람 확인. 시작 코드는 수리가 필요 없었다 (첫 생성 통과).
