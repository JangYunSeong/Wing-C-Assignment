# hidden/cases — 과제별 숨은 테스트 자리

이 폴더는 템플릿에서 비어 있다. 과제마다 `gen_hidden` 이 쓴 케이스(오버레이 `kind=hidden`)가 채점용 이미지 조립 때 들어온다
(`docs/design/spec-to-code.md` §4, `manifest.yaml` 의 `hidden_layout`).

규칙
- 패키지 `com.example.service.grading.cases`, 클래스 이름 `*Test`. `GraderSupport` 를 상속하고 공개 계약(HTTP)으로만 판정한다.
- 운영 조건·지연·주입 값은 코드에 박지 않고 `GraderParams` 로 읽는다 (값은 HiddenPlan → `grader-params.properties`).
- 지연·오류 주입은 `GraderJdbcHarness` 로만 하고 `finally` 에서 `GraderJdbcHarness.reset()` 을 부른다.
- 동시 요청은 `Burst.fire`, 진단 줄은 `GraderSupport.diag` / `Burst.summary` (판정에는 쓰지 않음).
- 단언 설명에 의도한 실패 사유를 `Reasons.as("<code>", …)` 로 붙인다. 코드는 `manifest.yaml` 의 `failure_reasons` 안의 것만.
- 하네스(`grading/*.java`)·자체 점검(`grading/selftest`)은 고치지 않는다 (잠김).
