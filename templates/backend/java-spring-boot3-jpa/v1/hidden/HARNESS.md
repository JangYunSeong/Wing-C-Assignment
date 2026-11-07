# 채점 하네스 API — 숨은 테스트 케이스 작성용 (backend/java-spring-boot3-jpa v1)

P1 내부 문서다 (`gen_hidden` 프롬프트에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

이 문서에 적힌 클래스·메서드·상수만 있다. 없는 이름을 만들어 쓰지 않는다 (필요하면 케이스 파일 안에 private 메서드로).

## 케이스 파일

- 위치 `src/test/java/com/example/service/grading/cases/<이름>Test.java`, 패키지 `com.example.service.grading.cases`.
- **케이스 하나 = HiddenPlan 케이스 하나 = 테스트 클래스 하나.** 클래스의 `@DisplayName` 에 케이스 id(`hc1` …)를 쓴다.
- `GraderSupport` 를 상속한다. 지원자 구현(`com.example.service.*` 중 `grading` 밖)을 import 하지 않는다 — 공개 계약(HTTP)으로만 판정.
- 조절값 파일(`grader-params.properties`)은 쓰지 않는다 — 빌드 파이프라인이 HiddenPlan 에서 쓴다.

## GraderSupport (상속)

- `@SpringBootTest(RANDOM_PORT)` — 지원자 저장소 그대로 띄운다. 운영 조건(`pool_size` · `connection_timeout_ms` · `server_threads`)은 자동으로 Spring 속성에 들어간다.
- `protected HttpResponse<String> send(String method, String path, Map<String, String> headers, String jsonBody)` — body 가 null 이면 본문 없음
- `protected HttpResponse<String> get(String path)`
- `send` · `get` 은 `IOException, InterruptedException`, `json` · `jsonObject` 는 `IOException`, `await` 는 `Exception` 을 던진다 (검사 예외) — 테스트 메서드와 이것들을 부르는 private 헬퍼에는 `throws Exception` 을 붙인다. `Burst.Call` 람다는 `throws Exception` 이라 그대로 써도 된다. `Predicate`·`Function` 같은 표준 람다 안에서는 이것들을 부르지 않는다.
- `protected <T> T json(String body, TypeReference<T> type)` · `protected Map<String, Object> jsonObject(String body)`
  - 목록 응답: `List<Map<String, Object>> rows = json(resp.body(), new TypeReference<>() { });` (`import com.fasterxml.jackson.core.type.TypeReference;`). 미리 만든 타입 상수는 없다.
- `protected <T> T await(Callable<T> poll, Predicate<T> done)` — done 이 참이 될 때까지 `AWAIT_TIMEOUT` 동안 다시 부르고 마지막 값을 돌려준다 (비동기 설계를 공정하게)
- `protected static Instant parseTime(Object value)` · `protected static void diag(String format, Object... args)` (진단 출력 — 판정에 쓰지 않음)
- 상수: `REQUEST_TIMEOUT`, `AWAIT_TIMEOUT`, `JSON`(ObjectMapper)

## GraderParams — 조절값 (값을 코드에 박지 않는다)

- `GraderParams.intValue("concurrency", 32)` · `GraderParams.longValue("query_delay_ms", 20)` · `GraderParams.longList("<name>")` · `GraderParams.has("<name>")`
- 이름은 HiddenPlan.harness 의 키. 기본값은 조절값 파일이 없을 때만 쓰인다.

## GraderJdbcHarness — DB 왕복 지연·오류 주입 (`static` 필드, 끝나면 `finally` 에서 `GraderJdbcHarness.reset()`)

- `GraderJdbcHarness.queryDelayMs = GraderParams.longValue("query_delay_ms", 20);` — SQL 문마다 지연 (동시 요청이 실제로 겹치게)
- `GraderJdbcHarness.failStatements = Pattern.compile("^\\s*update\\s+orders\\b", Pattern.CASE_INSENSITIVE);` — 맞는 SQL 실행을 실패시킨다
- `GraderJdbcHarness.failCommitAfter = Pattern.compile(...);` — 맞는 SQL 을 실행한 연결의 다음 commit 을 실패시킨다 (검사는 통과했는데 커밋 단계에서 롤백)
- `resetStats()` · `MAX_DEMAND_PER_THREAD` · `ACQUISITIONS` · `INJECTED_FAILURES` — 진단용 (판정에 쓰지 않음)
- **SQL 정규식의 테이블 이름은 계약 그대로** — 계약 데이터 절의 `` #### `Delivery` — 테이블 `delivery` `` 처럼 적힌 이름. API 경로(`/deliveries`)나 엔티티 이름에서 짐작하지 않는다 (복수형·대소문자가 스택마다 다름). 빌드 파이프라인이 계약 밖 테이블을 가리키면 다시 쓰게 한다.

## Burst — 동시 요청

- `List<Burst.Outcome> Burst.fire(int n, IntUnaryOperator expectedStatus, Burst.Call call, Duration wait)`
  - n 건을 한꺼번에 보낸다. `call.send(i)` 는 i 번째 요청을 보내고 HTTP 상태 코드를 돌려준다. 시간 초과·연결 오류는 -1.
  - `expectedStatus.applyAsInt(i)` = i 번째 요청의 기대 상태 코드.
- `Burst.wrong(outcomes)` — 기대와 다른 수 · `Burst.summary(label, n, outcomes)` — 진단 줄
- `Burst.Outcome` 는 **record** — 값은 접근자로 읽는다: `o.index()` · `o.expected()` · `o.actual()`(상태 코드, -1 = 시간 초과·연결 오류) · `o.millis()` · `o.error()` · `o.ok()`(actual == expected). 필드 직접 접근(`o.actual`)은 컴파일되지 않는다.

## 판정 규칙

- **기대 상태 코드는 계약 `responses` 에 있는 것만.** 계약이 정하지 않은 상황(주입한 DB 오류 등)의 응답 코드는 정확한 값을 요구하지 않는다 — `isNotEqualTo(200)` 처럼 계약이 말하는 만큼만.
- JSON 숫자는 크기에 따라 `Integer`·`Long` 으로 읽힌다 — `((Number) row.get("id")).longValue()` 로 바꿔 비교한다 (`isEqualTo(105L)` 를 `Integer` 와 비교하면 실패).
- 기대 결과는 HiddenPlan 케이스의 `expect` 에서만 나온다. `expect` 에 없는 조건(응답 시간·로그 순서 등)을 더 요구하지 않는다.

## Reasons — 의도한 실패 사유

- `assertThat(x).as(Reasons.as("<code>", "설명 %d", id))` — 실패 메시지에 `[reason:<code>]` 가 붙는다. 코드는 manifest `failure_reasons` 안의 것만.

## 데이터

- **케이스마다 받은 id 범위만** 쓴다 — 숨은 테스트 계획의 케이스 아래 `데이터: <엔티티> id a~b (…)` 줄. 범위는 빌드 파이프라인이 계약 시드에서 나눠 준 것이라 행이 반드시 있고, 케이스끼리·지원자 테스트(작은 id)와 겹치지 않는다. 범위 밖 id 를 쓰지 않고, 행을 새로 만들지 않는다. 케이스 시나리오 글에 다른 id 가 적혀 있어도 이 범위가 우선이다.
- 행의 처음 값은 그 줄에 적힌 것(`처음 status=READY` 등)만 가정한다. `버스트` 라고 적힌 범위는 동시 요청마다 다른 id 를 하나씩 쓴다 (범위 크기 = 동시 요청 수 × 버스트 수).
- 상태를 바꾸는 케이스는 그 케이스 안에서 필요한 상태를 만든 뒤 확인한다 (다른 케이스 순서에 기대지 않음).

## 예시 (모양만 — 과제마다 계약에 맞춘다)

```java
package com.example.service.grading.cases;

import java.time.Duration;
import java.util.Map;

import com.example.service.grading.Burst;
import com.example.service.grading.GraderJdbcHarness;
import com.example.service.grading.GraderParams;
import com.example.service.grading.GraderSupport;
import com.example.service.grading.Reasons;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

@DisplayName("hc1 운영 피크와 같은 수의 동시 요청")
class PeakBurstTest extends GraderSupport {

    static final long FIRST_ID = 105; // 케이스 "데이터: … id 105~200 (…, 버스트)" 줄의 범위 시작

    @Test
    void 모든_요청이_정상_응답() throws Exception {
        int n = GraderParams.intValue("concurrency", 32);
        GraderJdbcHarness.queryDelayMs = GraderParams.longValue("query_delay_ms", 20);
        try {
            var outcomes = Burst.fire(n, i -> 200,
                    i -> send("PATCH", "/items/" + (FIRST_ID + i), Map.of("X-Actor", "grader"), "{\"state\":\"B\"}").statusCode(),
                    Duration.ofSeconds(60));
            diag("%s", Burst.summary("hc1", 1, outcomes));
            assertThat(Burst.wrong(outcomes)).as(Reasons.as("pool_acquire_timeout", "기대와 다른 응답 수")).isZero();
        } finally {
            GraderJdbcHarness.reset();
        }
    }
}
```
