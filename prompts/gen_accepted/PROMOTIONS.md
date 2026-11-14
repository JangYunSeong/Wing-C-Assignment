# gen_accepted 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 평가: `eval/starter/run_generate.py --loop` (git 밖 결과).

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-07 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 만들 때 CURRENT 로 둠 (5단계) | 5단계 구현 ("1,2 수행하고 5단계 진행해줘") |
| 2026-11-07 | CURRENT v1 → **v2** (수리면 자기 이전 출력을 받아 고칠 파일만) | opencode:qwen3.8-flash (머리말 — 스모크는 gpt-4.1) | 아래 v2 절: 배송 감사 루프에서 수리 2건 모두 고칠 파일만 바뀌고 그 한 번에 해결 (근거 1회 — 해가 될 가능성이 낮아 승격). 비용 $0.18 | 사용자 확인 (2026-11-07 "1,2,3 진행하고" — 수리판 승격 제안에) |
| 2026-11-10 | CURRENT v2 → **v6** (v6 sha256 3d85085465a5…) — 일반 원칙(v3)·테스트 먼저(v4) + 출력 상한(v5·v6) | opencode:qwen3.8-flash (머리말 — 알파는 gpt-4.1) | 전체 알파 2026-11-08 (`2026-11-08-alpha-full-ae023`·`-sg004`, gpt-4.1) 이 plan_contract v11 · gen_public v1 · gen_hidden v3 · gen_starter v7 · gen_naive v4 · gen_accepted v4 를 고정해 두 공고 review — 최신 판은 이 판들에 출력 상한만 바꾼 것(지시문 동일). CURRENT 가 테스트 먼저(v7·v4) 이전 판에 남아 있어 버전 고정 없이 돌린 백엔드 알파(2026-11-09 full2)는 옛 지시문을 썼다. 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) — 같은 승격이 브랜치 chore/generator-current-latest(2026-11-09, 미병합)에 있었다 | 사용자 ("백엔드 생성기 CURRENT 를 최신으로 맞출지 … 이거 진행해주고") |
| 2026-11-12 | CURRENT v6 → **v7** (v7 sha256 45ea63814634…) — 코드 뼈대 규칙: 이름·매개변수·매핑·요청 record 는 두고 몸통만, 계약 경로 다시 매핑 금지 | opencode:qwen3.8-flash (머리말) | 템플릿 java v2(2026-11-11) 뼈대 도입 후 참조 구현 프롬프트에 뼈대 규칙이 없었음 — 실패는 0건(정적 검사 _skeleton_kept 가 막음). 백엔드 빌드 16건(qwen, 2026-11-10~) 대조 — 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09). 평가는 다음 백엔드 알파가 첫 평가 | 사용자 (2026-11-12 Java·React 프롬프트 대조 검토 — '권하는 방식: Java 생성기 프롬프트 새 판을 한 번에') |
| 2026-11-14 | CURRENT v7 → **v8** (v8 sha256 e47dc9b9bec2…) — 사례 괄호 '(목록을 페이지 객체로 감싸기 등)' 삭제 | opencode:qwen3.8-flash (머리말) | 근거: #185 (gen_starter v10 과 같은 변경). 첫 평가(Java 알파, qwen, 이전 판으로 review 까지 간 명세 재사용): `2026-11-14-alpha-g185-lost-kakao003` review 3회차 $0.0627 (이전 판 같은 명세 review $0.0507 — 함정 둘 숨은 테스트가 잡음, ⑩ r1 변형 셋 잡음) · `…-g185-nplus1-hana006` 실패(공개 테스트 rows 의 조인 원천 행 — plan_contract v29 가 고침) · `…-g185b-nplus1-hana006`(v29) 실패 — gen_accepted JSON 파싱 실패 2/2(qwen 간헐, 판과 무관), 그 전 단계 공개 테스트는 첫 답 통과. 생성기 CURRENT 는 최신 판 (불변식 7 예외) | 사용자 (2026-11-14 "스택 하나씩 진행해줘" — 마일스톤 2 #185) |

## v2 (2026-11-07, CURRENT — 위 표)

v1 + 수리면 자기 이전 출력을 받아 고칠 파일만 출력 (코드가 합침, 빈 content 는 삭제). 이전 루프에서 숨은 테스트가 import 하나를 빠뜨렸는데 수리가 처음부터 다시 써 다른 문제가 생겼다.

- 수리 루프 1회 (gpt-4.1, 배송 감사 v5 계약 run 1, gen_starter v3·gen_hidden v2·gen_naive v2·gen_accepted v2 고정, `data/eval/build/2026-11-07-loop-repair-prev`, $0.18): 수리 2건 모두 **고칠 파일만** 바뀜 — 숨은 테스트 3파일 중 1개(테스트 자체 예외가 난 hc2), d1 2파일 중 1개(기존 테스트를 깬 서비스). 두 문제 모두 그 한 번에 고쳐짐. 남은 실패는 다시 만든 hc2 의 주입이 너무 넓음(`failCommitAfter = "delivery|delivery_audit_log"` — 실패 기록 INSERT 의 커밋까지 막아 어떤 설계도 통과 못 함) → 루프가 '숨은 테스트·계약 의심'으로 사람 확인.
