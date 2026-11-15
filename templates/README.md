# templates — 시작 코드 템플릿 (스택별 · 버전 고정)

설계: `docs/design/starter-templates.md` §3·§6.1, `docs/design/spec-to-code.md` §4.1. 레지스트리·점검: `src/task_spec/starter/templates.py`.

```
templates/<직군>/<스택>/CURRENT          vN 한 줄 — 바꾸는 것은 평가 통과 뒤
templates/<직군>/<스택>/vN/
  project/        동작하는 최소 프로젝트 (과제와 무관한 뼈대) — 템플릿·응시용 이미지에 들어가는 유일한 부분
  manifest.yaml   명령 · fill_slots · locked · reserved · hidden_layout · limits · image · harness_params · failure_reasons · stack_conditions
  README.tpl.md   지원자 README 틀 (render_readme 가 승인된 명세 문장·계약으로 채움, LLM 없음)
  hidden/harness/ 잠긴 채점 하네스 (지연·오류 주입 · 연결 추적 · 운영 설정 덮어쓰기 · 동시 요청 실행기)
  hidden/selftest/ 하네스 자체 점검 — 실패하면 HARNESS-ERR
  hidden/cases/   과제별 숨은 테스트 자리 (비어 있음 — 오버레이 kind=hidden)
```

규칙
- **`vN` 에서 무엇이 고정인가** [제안: 2026-11-14 #176 댓글 — 오버레이가 기대는 것만 고정]. 고정된 것을 바꿀 일이 있으면 `vN+1` 을 더한다
  (프롬프트 vN 과 같은 규칙). 검사·수리 전용 칸은 같은 `vN` 에서 고친다 — 오버레이가 기대지 않고, 지금도 코드(커밋)로 바뀌던 지식이다.

  | 같은 vN 에서 고치지 않음 | 같은 vN 에서 고칠 수 있음 (검사·수리 전용) |
  |---|---|
  | `project/`·`hidden/`·`README.tpl.md` | `code_pitfalls` |
  | 이미지·채점 설정에 들어가는 칸 — commands·locked·reserved·hidden_layout·limits·image·harness_params·`hidden_rules.case_id_pattern` | `repair_terms` (`server_side` 포함) |
  | 생성기 입력 — fill_slots·stack_conditions·`failure_reasons` 의 code·description·how | `hidden_rules` 의 검사 칸 — `reason_not_on`·`reason_labels`·`http_calls`·`observable_params` |
  | 평가 키트가 다시 읽는 칸 — `failure_reasons` 의 description (묶음 확정이 template_ref 로 다시 읽는다) | `failure_reasons` 의 `requires`·`feasible`·`assert_on`·`assert_not_on` |

  빌드 기록(`common_task_build.manifest_sha256`)과 알파 `report.json`(`template.manifest_sha256`)에 매니페스트 해시를 남겨, 어느 판의 검사·수리로
  만든 빌드인지 가린다 (template_ref 는 id@vN 뿐).
- **템플릿 이미지·응시용 이미지에는 `project/` 와 의존성 캐시만** [확정: 2026-11-07 사용자]. `hidden/` 은 채점용 레이어에서만 더하고,
  `manifest.yaml` 은 이미지에 넣지 않는다 — 채점 이미지에는 빌드 때 매니페스트에서 만든 `config.json`(locked·reserved·명령·layout·`case_id_pattern`)이
  들어간다 (`starter/images.py`). 채점이 무엇을 노리는지(조절값·실패 사유)가 보이지 않게. 금지 경로는 `candidate_layer_rules` 가 계산한다.
- 과제별 정답(숨은 테스트 케이스·참조 구현)은 여기 두지 않는다 — DB 오버레이(#52).
- 운영 값(풀 크기·스레드 등)을 `project/` 설정에 박지 않는다 — 채점 때 하네스가 넣는다.

| 템플릿 | 근거 | 확인 |
|---|---|---|
| `backend/java-spring-boot3-jpa` v1 | 배송 감사 손 예시(Java, git 밖)에서 뼈대·하네스를 과제와 무관하게 일반화 | Gradle 빌드 + 뼈대 테스트 1 + 하네스 자체 점검 6 통과 (2026-11-07, 개발 서버 — 네트워크 없는 이미지 검증은 #55) |
| `data/python-duckdb` v2 | v1 + 하네스 읽기의 뜻을 형식마다 같게 — 없는 경로는 FileNotFoundError(`ws.exists` 로 있는지), 칸 수가 머리글과 다른 CSV 줄은 깨진 파일 예외 (#187 데이터 알파 g187-de-kb002: 크래시 뒤 '출력 없음'을 빈 목록으로 돌려줘 허용 구현 셋이 모두 떨어짐). 크래시를 요청했는데 끝까지 성공하면 `CrashNotTriggered`(케이스 전제가 서지 않음 — 상한은 파일 하나의 크기, g187b-de-kb002). `project/` 는 v1 과 같음 · CURRENT 는 평가 뒤 | 하네스 자체 점검 10 통과(기존 7 + 새 3, 2026-11-15) |
