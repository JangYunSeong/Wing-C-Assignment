# gen_hidden 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 평가: `eval/starter/run_generate.py --loop` (git 밖 결과).

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-07 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 만들 때 CURRENT 로 둠 (5단계) | 5단계 구현 ("1,2 수행하고 5단계 진행해줘") |

## v2 (2026-11-07, 아직 CURRENT 아님)

v1 + 수리면 자기 이전 출력을 받아 고칠 파일만 출력 (코드가 합침, 빈 content 는 삭제). 이전 루프에서 숨은 테스트가 import 하나를 빠뜨렸는데 수리가 처음부터 다시 써 다른 문제가 생겼다.

- 수리 루프 1회 (gpt-4.1, 배송 감사 v5 계약 run 1, gen_starter v3·gen_hidden v2·gen_naive v2·gen_accepted v2 고정, `data/eval/build/2026-11-07-loop-repair-prev`, $0.18): 수리 2건 모두 **고칠 파일만** 바뀜 — 숨은 테스트 3파일 중 1개(테스트 자체 예외가 난 hc2), d1 2파일 중 1개(기존 테스트를 깬 서비스). 두 문제 모두 그 한 번에 고쳐짐. 남은 실패는 다시 만든 hc2 의 주입이 너무 넓음(`failCommitAfter = "delivery|delivery_audit_log"` — 실패 기록 INSERT 의 커밋까지 막아 어떤 설계도 통과 못 함) → 루프가 '숨은 테스트·계약 의심'으로 사람 확인.
