# {{title}}

{{scenario}}

## 요구 사항

{{requirements}}

## 공개 수용 기준

{{acceptance_public}}

## API 계약

{{contract}}

## 제출물

{{deliverables}}

## 코드 구조

- Java 17 · Spring Boot 3.3 · Spring Data JPA · H2(인메모리) · Gradle
- 애플리케이션 코드는 `src/main/java/com/example/service/` 아래, 스키마·시드는 `src/main/resources/schema.sql` · `data.sql`

## 실행

```bash
./gradlew bootRun     # 서버 실행 (기본 포트 8080)
./gradlew test        # 테스트
```

## 채점 방법

채점 때 이 계약을 기준으로 한 HTTP 테스트가 추가로 실행된다. 위 형식과 시드 데이터는 바꾸지 않는다.

## 진행 방식

{{process}}
