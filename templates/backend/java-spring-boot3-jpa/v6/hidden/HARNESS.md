# 채점 하네스 API — 숨은 테스트 케이스 작성용 (backend/java-spring-boot3-jpa v1)

P1 내부 문서다 (`gen_hidden` 프롬프트에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

이 문서에 적힌 클래스·메서드·상수만 있다. 없는 이름을 만들어 쓰지 않는다 (필요하면 케이스 파일 안에 private 메서드로).

## 케이스 파일

- 위치 `src/test/java/com/example/service/grading/cases/<이름>Test.java`, 패키지 `com.example.service.grading.cases`.
- **케이스 하나 = HiddenPlan 케이스 하나 = 테스트 클래스 하나.** 클래스의 `@DisplayName` 에 케이스 id(`hc1` …)를 쓴다.
- **`ContractCase` 를 상속한다** (`GraderSupport` 를 직접 상속하지 않는다). `ContractCase` 는 빌드 파이프라인이 이 과제의 계약에서 만든
  도우미로 이 문서 끝에 소스가 붙어 있다 — 케이스별 id 범위 상수(`HC1_FIRST` …), 계약 테이블 이름(`TABLE_…`), 처음 값별 다음 상태
  (`NEXT_OK_<엔티티>_<처음 값>` 허용되는 값 · `NEXT_BAD_…` 거절되는 값 — 계약 state 칸에서), 계약 API 호출 함수와 200 본문을 계약 모양
  (목록/객체)대로 읽는 `…Body` 함수, 운영 피크 `peak(…)`, 오류 주입 `injectCommitFailure(verb, TABLE_…)`·`injectStatementFailure(verb, TABLE_…)`,
  숫자 비교 `num(…)`, 요청 헤더 값(`HEADER_<헤더>_OK` 규칙·권한을 통과하는 값 · `HEADER_<헤더>_DENIED` 권한이 거절되는 값 —
  계약 공개 테스트 예시에서)과 통과 값 묶음 `okHeaders()`. **id·경로·응답 파싱·상태 전이·동시 요청 시간 한도·요청 헤더 값을
  손으로 쓰지 말고 이것들을 쓴다** — 계약과 어긋날 수 없게.
- **요청 헤더 값(요청자 등)을 지어내지 않는다.** 기능 단계(저장·기록)에 닿아야 하는 요청은 `okHeaders()`, 권한 거절을 시험하는
  요청만 `HEADER_<헤더>_DENIED` 를 쓴다. 지어낸 값은 권한에서 거절돼 요청이 기능에 닿지 못하고, 기능 없는 시작 코드도 케이스를 통과한다.
  도우미에 헤더 상수가 없으면(계약에 헤더가 없는 엔드포인트) 헤더 자리에 `Map.of()` 를 넘긴다. `ContractCase.java` 와 조절값 파일(`grader-params.properties`)은 쓰지 않는다
  (빌드 파이프라인이 쓴다).
- 지원자 구현(`com.example.service.*` 중 `grading` 밖)을 import 하지 않는다 — 공개 계약(HTTP)으로만 판정.

## GraderSupport (상속)

- `@SpringBootTest(RANDOM_PORT)` — 지원자 저장소 그대로 띄운다. 운영 조건(`pool_size` · `connection_timeout_ms` · `server_threads`)은 자동으로 Spring 속성에 들어간다. 요청 한도 `REQUEST_TIMEOUT` 은 **`java.time.Duration`** 이다 (`request_timeout_s` 를 주지 않으면 30초와 연결 대기 한도 × 2 + 5초 중 긴 쪽). `Duration.ofSeconds(REQUEST_TIMEOUT)` 처럼 다시 감싸지 않는다 — 운영 피크는 `peak(…)` 가 이미 넣어 둔다. 이 한도는 `Burst` 의 마감(`Burst.fire(…, REQUEST_TIMEOUT)`)으로 잰다 — 그 밖의 요청(데우기·조회 확인)은 클라이언트가 최소 30초 기다리므로 시간 초과를 재려면 `Burst` 로 보낸다.
- 케이스 사이는 하네스가 정리한다 — 테스트가 끝나면 서버가 빌 때까지 기다리고, 비지 않으면(앞 케이스의 교착 등) 다음 케이스를 새 앱에서 돌린다. 케이스가 앞 케이스의 남은 요청을 기다리거나 치우는 코드를 쓰지 않는다.
- `protected HttpResponse<String> send(String method, String path, Map<String, String> headers, String jsonBody)` — body 가 null 이면 본문 없음
- `protected HttpResponse<String> get(String path)`
- `send` · `get` 은 `IOException, InterruptedException`, `json` · `jsonObject` 는 `IOException`, `await` 는 `Exception` 을 던진다 (검사 예외) — 테스트 메서드와 이것들을 부르는 private 헬퍼에는 `throws Exception` 을 붙인다. `Burst.Call` 람다는 `throws Exception` 이라 그대로 써도 된다. `Predicate`·`Function` 같은 표준 람다 안에서는 이것들을 부르지 않는다.
- `protected <T> T json(String body, TypeReference<T> type)` · `protected Map<String, Object> jsonObject(String body)`
  - 목록 응답: `List<Map<String, Object>> rows = json(resp.body(), new TypeReference<>() { });` (`import com.fasterxml.jackson.core.type.TypeReference;`). 미리 만든 타입 상수는 없다.
- `protected <T> T await(Callable<T> poll, Predicate<T> done)` — done 이 참이 될 때까지 `AWAIT_TIMEOUT` 동안 다시 부르고 마지막 값을 돌려준다 (비동기 설계를 공정하게)
- `protected static Instant parseTime(Object value)` — 형식이 틀리면 **null** (예외 아님) · `protected static void diag(String format, Object... args)` (진단 출력 — 판정에 쓰지 않음)
- 상수: `REQUEST_TIMEOUT`·`AWAIT_TIMEOUT` (둘 다 `java.time.Duration`), `JSON`(ObjectMapper)

## GraderParams — 조절값 (값을 코드에 박지 않는다)

- `GraderParams.intValue("concurrency", 32)` · `GraderParams.longValue("query_delay_ms", 20)` · `GraderParams.longList("<name>")` · `GraderParams.has("<name>")`
- 이름은 HiddenPlan.harness 의 키. 기본값은 조절값 파일이 없을 때만 쓰인다.

## GraderJdbcHarness — DB 왕복 지연·오류 주입 (`static` 필드, 끝나면 `finally` 에서 `GraderJdbcHarness.reset()`)

- `GraderJdbcHarness.queryDelayMs = GraderParams.longValue("query_delay_ms", 20);` — SQL 문마다 지연 (동시 요청이 실제로 겹치게)
- `GraderJdbcHarness.failStatements = Pattern.compile("^\\s*update\\s+orders\\b", Pattern.CASE_INSENSITIVE);` — 맞는 SQL 실행을 실패시킨다
- `GraderJdbcHarness.failCommitAfter = Pattern.compile(...);` — 맞는 SQL 을 실행한 연결의 다음 commit 을 실패시킨다 (검사는 통과했는데 커밋 단계에서 롤백)
- `resetStats()` · `MAX_DEMAND_PER_THREAD` · `ACQUISITIONS` · `INJECTED_FAILURES` — 진단용 (판정에 쓰지 않음)
- **주입은 좁게** — 정규식은 `^\\s*update\\s+<테이블>\\b` 처럼 SQL 종류 + 테이블로 시작한다. 테이블 이름만 쓰면(`"orders|order_history"`) 그 테이블을 읽거나 기록하는 모든 문이 걸려 실패 기록 쓰기까지 막힌다. 숨은 요구가 말하는 그 단계(예: 상태 변경 저장)만 주입하고, 기록 쓰기는 막지 않는다.
- **SQL 정규식의 테이블 이름은 계약 그대로** — 계약 데이터 절의 `` #### `Delivery` — 테이블 `delivery` `` 처럼 적힌 이름. API 경로(`/deliveries`)나 엔티티 이름에서 짐작하지 않는다 (복수형·대소문자가 스택마다 다름). 빌드 파이프라인이 계약 밖 테이블을 가리키면 다시 쓰게 한다.

## Burst — 동시 요청

- 기대 상태 함수의 타입은 JDK 의 `java.util.function.IntUnaryOperator` 다 (`Burst` 안의 타입이 아니다) — 변수로 두지 말고 `i -> 200` 처럼 인자 자리에 람다로 쓴다.
- **운영 피크 재현은 `List<Burst.Outcome> Burst.rounds(int concurrency, int rounds, IntUnaryOperator expectedStatus, Burst.Call call, Duration wait)`** — `concurrency` 건씩 한꺼번에, `rounds` 번 차례로. index 는 회차를 이어 0 ~ `concurrency × rounds - 1` (버스트마다 다른 행은 `FIRST_ID + index`). **`concurrency × rounds` 건을 한 번에 보내지 않는다** — 운영 조건보다 큰 부하라 올바른 설계도 시간 초과가 난다.
- `List<Burst.Outcome> Burst.fire(int n, IntUnaryOperator expectedStatus, Burst.Call call, Duration wait)`
  - n 건을 한꺼번에 보낸다. `call.send(i)` 는 i 번째 요청을 보내고 HTTP 상태 코드를 돌려준다. 시간 초과·연결 오류는 -1 — `wait` 안에 안 끝난 요청도 예외가 아니라 -1 이다 (테스트에서 `TimeoutException` 을 잡지 않는다).
  - `expectedStatus.applyAsInt(i)` = i 번째 요청의 기대 상태 코드.
- `Burst.failures(outcomes)` — **시간 초과(-1)·5xx 수** (연결 고갈·교착 같은 운영 조건 실패) · `Burst.mismatches(outcomes)` — 그 밖의 기대와 다른 상태 수 (규칙·계약 불일치)
- `Burst.wrong(outcomes)` — 기대와 다른 수 전부 (둘의 합 — 사유를 붙이지 않는다) · `Burst.summary(label, n, outcomes)` — 진단 줄
- `Burst.Outcome` 는 **record** — 값은 접근자로 읽는다: `o.index()` · `o.expected()` · `o.actual()`(상태 코드, -1 = 시간 초과·연결 오류) · `o.millis()` · `o.error()` · `o.ok()`(actual == expected). 필드 직접 접근(`o.actual`)은 컴파일되지 않는다.

## 판정 규칙

- **기대 상태 코드는 계약 `responses` 에 있는 것만.** 계약이 정하지 않은 상황(주입한 DB 오류 등)의 응답 코드는 정확한 값을 요구하지 않는다 — `isNotEqualTo(200)` 처럼 계약이 말하는 만큼만.
- JSON 숫자는 크기에 따라 `Integer`·`Long` 으로 읽힌다 — `((Number) row.get("id")).longValue()` 로 바꿔 비교한다 (`isEqualTo(105L)` 를 `Integer` 와 비교하면 실패).
- **예외가 나지 않음을 확인할 때는 `assertThatCode(() -> …).doesNotThrowAnyException()`** — `assertThat(() -> …)` 처럼 람다를 `assertThat` 에 넣으면 AssertJ 에서 어느 오버로드인지 모호해 컴파일되지 않는다. 시각 형식 확인은 `assertThat(parseTime(값)).isNotNull()` (형식이 틀리면 null).
- 기대 결과는 HiddenPlan 케이스의 `expect` 에서만 나온다. `expect` 에 없는 조건(응답 시간·로그 순서 등)을 더 요구하지 않는다.
- **의도한 사유는 그 실패만 걸리는 단언에 붙인다.** 운영 조건 사유(`pool_acquire_timeout` 등)는 `Burst.failures(...)` 단언에, 기록 사유는 그 기록을 보는
  단언에. `Burst.wrong(...)` 처럼 모든 실패를 세는 단언에 사유를 붙이지 않는다 — 500·시간 초과·규칙 불일치가 모두 같은 사유로 보인다.
- **저장 단계 실패(롤백) 사유의 케이스는 `injectCommitFailure("update", TABLE_<대상>)`(또는 `GraderJdbcHarness.failCommitAfter`·`failStatements`)로 실패를 일으킨다.**
  숨은 테스트 계획의 케이스 아래 `일으키는 법` 줄이 있으면 시나리오 글보다 그것을 따른다. 규칙상 거절되는 요청(허용되지 않은
  전이)은 저장 전에 끝나 롤백될 변경이 없다 — 규칙을 통과하는 요청(처음 값에서 허용된 전이 `NEXT_OK_…`)에 주입한다.
- 기록이 "1건" 이면 `hasSize(1)` (`isNotEmpty()` 는 2건도 통과). 테스트 메서드끼리 서로 독립 — 순서에 기대지 않고 메서드마다 다른 행을 쓴다.

## Reasons — 의도한 실패 사유

- `assertThat(x).as(Reasons.as("<code>", "설명 %d", id))` — 실패 메시지에 `[reason:<code>]` 가 붙는다. 코드는 manifest `failure_reasons` 안의 것만.

## 데이터

- **케이스마다 받은 id 범위만** 쓴다 — `ContractCase` 의 `HC1_FIRST`·`HC1_LAST`·`HC1_ROWS` 상수 (숨은 테스트 계획의 케이스 아래 `데이터: <엔티티> id a~b (…)` 줄과 같음, 숫자를 다시 쓰지 않는다). 범위는 빌드 파이프라인이 계약 시드에서 나눠 준 것이라 행이 반드시 있고, 케이스끼리·지원자 테스트(작은 id)와 겹치지 않는다. 범위 밖 id 를 쓰지 않고, 행을 새로 만들지 않는다. 케이스 시나리오 글에 다른 id 가 적혀 있어도 이 범위가 우선이다.
- 행의 처음 값은 그 줄에 적힌 것(`처음 status=READY` 등)만 가정한다. `버스트` 라고 적힌 범위는 `peak(expectedStatus, i -> …)` 로 — 동시 요청 수만큼씩 버스트 수만큼 차례로 보내고, 요청마다 다른 id(`HC1_FIRST + i`)를 하나씩 쓴다 (범위 크기 = 동시 요청 수 × 버스트 수).
- 상태를 바꾸는 케이스는 그 케이스 안에서 필요한 상태를 만든 뒤 확인한다 (다른 케이스 순서에 기대지 않음).

## 예시 (모양만 — 과제마다 계약에 맞춘다. 상수·함수 이름은 이 과제의 `ContractCase` 것을 쓴다)

```java
package com.example.service.grading.cases;

import java.util.List;
import java.util.Map;

import com.example.service.grading.Burst;
import com.example.service.grading.GraderJdbcHarness;
import com.example.service.grading.GraderParams;
import com.example.service.grading.Reasons;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

@DisplayName("hc1 운영 피크와 같은 수의 동시 요청")
class PeakBurstTest extends ContractCase {

    @Test
    void 모든_요청이_정상_응답() throws Exception {
        GraderJdbcHarness.queryDelayMs = GraderParams.longValue("query_delay_ms", 20);
        try {
            List<Burst.Outcome> outcomes = peak(i -> 200,
                    i -> patchItemsId(HC1_FIRST + i, okHeaders(), "{\"state\":\"" + NEXT_OK_ITEM_A + "\"}").statusCode());
            diag("%s", Burst.summary("hc1", 1, outcomes));
            assertThat(Burst.failures(outcomes)).as(Reasons.as("pool_acquire_timeout", "시간 초과·서버 오류 수")).isZero();
            assertThat(Burst.mismatches(outcomes)).as("그 밖의 기대와 다른 응답 수").isZero();
            for (Burst.Outcome o : outcomes) {
                List<Map<String, Object>> rows = getItemsIdHistoryBody(getItemsIdHistory(HC1_FIRST + o.index()));
                assertThat(rows).as(Reasons.as("record_missing", "기록 1건 %d", o.index())).hasSize(1);
                assertThat(num(rows.get(0).get("itemId"))).as(Reasons.as("record_mismatch", "기록 대상 %d", o.index())).isEqualTo(HC1_FIRST + o.index());
            }
        } finally {
            GraderJdbcHarness.reset();
        }
    }
}
```
