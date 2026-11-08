# 공개 테스트 작성용 (backend/java-spring-boot3-jpa v1)

P1 내부 문서다 (`gen_public` 프롬프트에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

공개 테스트는 **공개 수용 기준(p\*)이 지켜지는지**를 확인하는 기능 테스트다. 시작 코드·참조 구현이 이 테스트를 보고 맞춘다(테스트 먼저).
숨은 요구(운영 조건·동시성·저장 실패)는 시험하지 않는다 — 그건 숨은 테스트 몫이다.

## 케이스 파일

- 위치 `src/test/java/com/example/service/publictests/<이름>Test.java`, 패키지 `com.example.service.publictests`.
- **계약의 공개 테스트(pt\*) 하나 = 테스트 클래스 하나.** 클래스 `@DisplayName` 에 pt id(`pt1` …)와 확인할 내용을 쓴다.
- **`PublicCase` 를 상속한다** — 빌드 파이프라인이 이 과제의 계약에서 만든 도우미로 이 문서 끝에 소스가 붙어 있다. 공개 테스트 몫 행 상수
  (`PUBLIC_<엔티티>_<처음 값>` — `long[]`), 계약 API 호출 함수와 200 본문을 계약 모양대로 읽는 `…Body` 함수, `num(…)`.
  **id·경로·응답 파싱을 손으로 쓰지 않는다.** `PublicCase.java` 와 `PersistenceCheckTest.java` 는 쓰지 않는다 (빌드 파이프라인이 쓴다).
- 지원자 구현(`com.example.service.*` 중 `grading`·`publictests` 밖)을 import 하지 않는다 — 공개 계약(HTTP)으로만 판정.

## 상속한 것 (GraderSupport)

- `send(method, path, headers, jsonBody)` · `get(path)` (검사 예외 — 테스트 메서드에 `throws Exception`)
- `json(body, new TypeReference<>() {})` · `jsonObject(body)` · `parseTime(value)` · `diag(format, args…)`

## 판정 규칙

- **기대 결과는 계약에서만**: 응답 상태 코드는 계약 `responses` 에 있는 것, 본문 필드·목록 모양은 계약 그대로, 상태를 가르는 규칙은 계약의
  전이 규칙 그대로. 계약에 없는 조건(응답 시간·로그 등)을 요구하지 않는다.
- **기록이 남는지는 계약의 조회 API 로** 확인한다 (DB 를 직접 읽지 않는다).
- 테스트 메서드마다 **서로 다른 행**(`PUBLIC_…[0]`, `[1]`, `[2]`)을 쓴다 — 테스트끼리 같은 DB 를 함께 쓰고 순서는 정해지지 않는다. 행의 처음
  값은 상수 이름대로만 가정한다.
- 동시 요청(`Burst`·`peak`), 오류 주입(`GraderJdbcHarness`), 조절값(`GraderParams`)을 쓰지 않는다 — 숨은 요구 영역이다.
- JSON 숫자는 `num(row.get("id"))` 로 long 으로 바꿔 비교한다.

## 예시 (모양만)

```java
package com.example.service.publictests;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

@DisplayName("pt1 상태를 바꾸면 기록이 최신순으로 남는다")
class StatusChangeRecordTest extends PublicCase {

    @Test
    void 성공한_변경이_기록된다() throws Exception {
        long id = PUBLIC_ITEM_READY[0];
        var r = patchItemsIdStatus(id, Map.of("X-Actor", "tester"), "{\"status\":\"SHIPPED\"}");
        assertThat(r.statusCode()).isEqualTo(200);
        List<Map<String, Object>> rows = getItemsIdHistoryBody(getItemsIdHistory(id));
        assertThat(rows).isNotEmpty();
        assertThat(num(rows.get(0).get("itemId"))).isEqualTo(id);
        assertThat(rows.get(0).get("result")).isEqualTo("SUCCESS");
    }
}
```
