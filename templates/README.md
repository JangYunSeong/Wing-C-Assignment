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
- `vN` 은 고치지 않는다 — 오버레이는 특정 버전 위에서만 맞는다. 바꿀 일이 있으면 `vN+1` 을 더한다 (프롬프트 vN 과 같은 규칙).
- **템플릿 이미지·응시용 이미지에는 `project/` 와 의존성 캐시만** [확정: 2026-11-07 사용자]. `hidden/`·`manifest.yaml`·`README.tpl.md` 는
  채점용 레이어에서만 더한다 — 채점이 무엇을 노리는지(조절값·실패 사유)가 보이기 때문. 금지 경로는 `candidate_layer_rules` 가 계산한다.
- 과제별 정답(숨은 테스트 케이스·참조 구현)은 여기 두지 않는다 — DB 오버레이(#52).
- 운영 값(풀 크기·스레드 등)을 `project/` 설정에 박지 않는다 — 채점 때 하네스가 넣는다.

| 템플릿 | 근거 | 확인 |
|---|---|---|
| `backend/java-spring-boot3-jpa` v1 | 배송 감사 손 예시(Java, git 밖)에서 뼈대·하네스를 과제와 무관하게 일반화 | Gradle 빌드 + 뼈대 테스트 1 + 하네스 자체 점검 6 통과 (2026-11-07, 개발 서버 — 네트워크 없는 이미지 검증은 #55) |
