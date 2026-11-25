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
  | `project/`·`hidden/`·`README.tpl.md` | `code_pitfalls`·`config_pitfalls` |
  | 이미지·채점 설정에 들어가는 칸 — commands·locked·reserved·hidden_layout·limits·image·harness_params·`hidden_rules.case_id_pattern` | `repair_terms` (`server_side`·`grading_env` 포함) · `harness_scope` (계약 검사 전용 — 하네스가 깔 수 있는 입력 환경·명령, #256) |
  | 생성기 입력 — fill_slots·stack_conditions·`failure_reasons` 의 code·description·how | `hidden_rules` 의 검사 칸 — `reason_not_on`·`reason_labels`·`http_calls`·`observable_params` |
  | 평가 키트가 다시 읽는 칸 — `failure_reasons` 의 description (묶음 확정이 template_ref 로 다시 읽는다) | `failure_reasons` 의 `requires`·`feasible`·`assert_on`·`assert_not_on`·`tunes` (빌드가 실험으로 맞출 숨긴 조절값, #301) |

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
| `data/python-duckdb` v2 | v1 + 하네스 읽기의 뜻을 형식마다 같게 — 없는 경로는 FileNotFoundError(`ws.exists` 로 있는지), 칸 수가 머리글과 다른 CSV 줄은 깨진 파일 예외 (#187 데이터 알파 g187-de-kb002: 크래시 뒤 '출력 없음'을 빈 목록으로 돌려줘 허용 구현 셋이 모두 떨어짐). 크래시를 요청했는데 끝까지 성공하면 `CrashNotTriggered`(케이스 전제가 서지 않음 — 상한은 파일 하나의 크기, g187b-de-kb002). `project/` 는 v1 과 같음. 같은 판에 검사·수리 칸 (2026-11-19, #256): `repair_terms.grading_env`(없는 실행 파일·셸 'not found'·네트워크 오류로 모든 구현이 같은 테스트에서 떨어지면 다시 만들지 않음 — 11-18 알파 woori001 의 crontab) · `harness_scope`(입력 환경은 file 만, 명령마다 파일 위치 옵션 — 스케줄 등록 명령·kind env 입력 환경을 계약 단계에서 되돌림) · **CURRENT v2** (2026-11-19 사용자 — 평가 통과가 아니라 v1 의 알려진 결함과 생성기 판 불일치 때문: 운영 builder 는 CURRENT v1 인데 CURRENT 생성기 gen_hidden_py v7 은 v2 하네스의 `CrashNotTriggered` 를 전제로 쓰고, v1 은 g187 의 '없는 출력 = 빈 목록' 결함을 그대로 가짐) | 하네스 자체 점검 10 통과(기존 7 + 새 3, 2026-11-15). 알파(개발 서버, qwen, `--template-version 2`) g187b·g187c kb002, g187c·g187d, 11-18 woori001 — 빌드 통과 0/3 이지만 실패 원인이 하네스가 아님 (크래시 케이스 상한 → gen_hidden_py v7 · 시작 코드 테스트의 NotImplementedError 단언 · 계약의 crontab 명령과 출력 위치). 11-18 기록 재생: grading_env 가 공개 테스트 재생성 대신 '채점 환경 확인 필요', harness_scope 가 그 계약의 f3·e2 만 되돌림(저장된 데이터 계약 7개 중 다른 6개는 0건) |
| `backend/java-spring-boot3-jpa` v3 | v2 + 공개 테스트 문서(`hidden/PUBLIC.md`) 예시의 요청 헤더를 지어낸 값(`Map.of("X-Actor", "tester")`) 대신 도우미 `okHeaders()` 로 (#191 — 생성기 프롬프트가 '문서 예시의 헤더 값은 따라 쓰지 않음' 으로 덮고 있었다). `project/`·하네스는 v2 와 같음 · CURRENT 는 평가 뒤 | 템플릿 자체 점검(`check_template`) 통과 (2026-11-15) — 코드 변경 없음 |
| `backend/java-spring-boot3-jpa` v4 | v3 + 빌드가 실험으로 맞출 수 있는 숨긴 조절값 (#301 — 2026-11-24 세션 유지 알파: 흔한 구현이 Hikari 기본 대기 30초에서 숨은 케이스를 통과했고, 같은 코드가 대기 한도·지연만 바꾸자 떨어짐). `connection_timeout_ms` 숨김·최소 250(Hikari 하한 — 1~249 는 기동 실패)·기본값 없음 (기본값을 두면 성립 검사가 요청 한도를 65초로 계산해 하네스 30초와 어긋남), `query_delay_ms` `default: 20`(성립 검사 = HARNESS.md 대체값, v3 까지 `default_expr: "0"`), `failure_reasons[].tunes`(`pool_acquire_timeout`·`request_timeout`) — 계약 LLM 이 넣으면 코드가 뺀다(`plan_contract.fit_tuned_params`). `project/`·`hidden/` 은 v3 과 같음 · CURRENT 는 빌드 보정(#301 PR 2)·알파 뒤 | 템플릿 자체 점검(`check_template`) 통과 · 하네스 자체 점검 11 통과 (2026-11-25, 개발 서버) |
| `backend/java-spring-boot3-jpa` v5 | v4 + 허용 구현 스택 조건 한 항목: 유일 키 충돌로 중복 요청을 막는 관용구 (#301 회귀 알파 2026-11-25 `alpha-r301-retry-naver002` — v4 에서 재시도 중복 명세를 다시 씀. 허용 구현 d2 가 직접 정한 `@Id String requestId` 엔티티를 `saveAndFlush` 로 넣었다. 이 호출은 merge(UPDATE)라 충돌이 나지 않아 숨은 hc1·hc2 가 '두 번 충전'으로 실패했다. 수리 단계에서 다시 만들어도 같은 꼴이 남아 회차 2 에서 실패. 11-14 v2 알파도 같은 재료에서 d2 가 실패 — 그때 코드는 `em.persist` + `catch DataIntegrityViolationException` 으로 보이지만 로그로 단정하지 않음). 항목에 담은 사실 셋 — 직접 정한 @Id 를 save·saveAndFlush 하면 merge · EntityManager 를 직접 부르면 충돌 예외가 Hibernate `ConstraintViolationException`(Repository 를 거칠 때만 `DataIntegrityViolationException`) · 충돌이 난 트랜잭션은 롤백 전용이라 처음 결과는 새 트랜잭션에서 읽음. 셋 모두 Spring Boot 3.3.5·Hibernate 6.5.3 Gradle 실험으로 확인. 매니페스트의 나머지 칸·`project/`·`hidden/` 은 v4 와 같음 · CURRENT 는 알파 뒤 | 템플릿 자체 점검(`check_template`) 통과 (2026-11-25). 수리 상황 재현 (개발 서버, qwen, 생성기는 CURRENT): 위 알파의 merge 꼴 d2 를 이전 판으로, 알파의 hc1·hc2 실패 메시지를 수리 메모(게이트 ④ 꼴)로 주고 d2 만 다시 만들어 같은 숨은 테스트로 검증 — v4 1/3 통과(실패 2회는 merge 꼴 그대로) · v5 3/3 통과 ($0.029). 이전 판 없이 처음부터 만들면 v4 2/2 · v5 3/3 모두 통과해 차이가 보이지 않음 — 이 함정은 처음 만들 때보다 버그 있는 이전 판을 고칠 때 굳는다. 3 대 3 이라 효과 크기는 알파로 다시 확인 |
| `frontend/react-vite-ts` v13 | v12 + 하네스가 고정 마디가 더 많은 경로를 먼저 — 매개변수 경로 처리기가 받은 주소에 같은 메서드의 더 구체적인 경로가 등록돼 있으면 넘긴다 (MSW 는 나중에 등록한 처리기를 앞에 두어, 계약 순서대로 `GET /items/list` 다음 `GET /items/{id}` 를 등록하면 목록 요청을 `id="list"` 로 붙잡았다 — 2026-11-15 프론트 빌드에서 모든 구현의 모든 테스트가 '요청이 오지 않음') · 문서 예시의 응답 본문을 `contract.ts` 의 `<Api>Ok` 타입으로 (#191). `project/` 는 v12 와 같음 · **CURRENT v13** (2026-11-15 사용자 — 아래 평가) | 하네스 자체 점검 통과 — 새 점검 '고정 경로가 매개변수 경로보다 먼저' 는 v12 하네스에서 보고된 증상 그대로 실패, v13 에서 통과 (2026-11-15, 개발 서버). 평가 알파(운영 서버, qwen, `--template-version 13`): `react13-stale-ae4013b`(늦은 검색 응답, 명세부터) **review** 회차 2 · `react13-prod-0476`(운영 승인 명세 `ct-0476eb58936e` 재사용) **review** 회차 0 — 같은 명세의 운영 v12 빌드(b1)는 ⑤ 공개 테스트 수리 1번 (코드 판도 달라 v13 만의 효과로 단정하지 않음) · `react13-prod-c0df`(운영 승인 명세 `ct-c0dfb030f21f`) failed — 공개 테스트가 `api.always` 에 응답 본문을 `{ json }` 없이 넣어 컴파일 실패 (`api.always` 문서는 v12 와 같음 — 템플릿 무관). 명세·계약 단계에서 멈춘 시도 3건·실행 환경 경로 문제 2건은 하네스까지 가지 않아 셈에서 뺌 |
| `frontend/react-vite-ts` v14 | v13 + 하네스의 변경 뒤 재조회 자동 응답(v9)이 **같은 신원**(`Authorization`·`Cookie` 헤더)의 요청만 — v13 까지는 메서드·경로·쿼리만 보아, 사용자 A 의 목록에 응답한 뒤 B 의 로그인(성공한 POST = 변경) 다음 온 B 의 같은 목록 요청을 재조회로 보고 A 의 목록으로 대신 응답했다. 로그아웃 뒤 다른 사용자 재료(`previous-user-data-after-logout`)의 숨은 hc1 이 허용 구현 d1·d2·흔한 구현 모두 'B 응답 전 A 항목이 보임'으로 떨어짐 (2026-11-18 `ct-10eb910705dd-b2`·`b3`, `ct-641fc8d7cf0c-b1` — 다른 계약 셋, 숨은 테스트 여섯 벌). `project/` 는 v13 과 같음 · **CURRENT v14** (2026-11-18 사용자 — 아래 평가. 토큰 갱신 재료 회귀 확인은 하지 않고 올림: 갱신 뒤 새 토큰으로 다시 받는 목록은 v14 에서 자동 응답하지 않으므로, 그 재료의 숨은 테스트가 자동 응답에 기대면 '요청이 오지 않음'으로 바뀔 수 있다 — 다음 토큰 갱신 빌드에서 확인) | 하네스 자체 점검 35 (기존 33 + 새 2) — node:22 컨테이너에서 v14 전부 통과, v13 하네스는 새 점검 '다른 신원의 같은 요청은 재조회가 아니다' 만 실패 (2026-11-18, 운영 서버). 실제 사례 재현: `ct-641fc8d7cf0c-b1` 의 숨은 hc1 + 허용 구현 d1 — v13 은 '재조회 자동 응답 GET /api/v1/purchases' 뒤 운영과 같은 문구로 실패, v14 는 통과 · d2 통과 · 흔한 구현은 v14 에서도 `other_user_data_shown` 으로 실패 (함정은 그대로 잡음). 평가 알파(운영 서버, qwen, `--template-version 14`, 운영 승인 명세 `ct-641fc8d7cf0c` 재사용 — 운영 v13 빌드는 같은 명세로 ④ 실패): `fe14-logout-641a` **review** 회차 1 · `fe14-logout-641b` **review** 회차 1 — 두 번 모두 d1·d2 hc1 통과, 흔한 구현 `other_user_data_shown` 실패 |
