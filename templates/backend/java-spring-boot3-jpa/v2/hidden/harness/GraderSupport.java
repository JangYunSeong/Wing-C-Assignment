package com.example.service.grading;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.function.Predicate;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import org.junit.jupiter.api.BeforeEach;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/**
 * 채점 케이스 공통 기반 (잠긴 하네스). 실제 서블릿 컨테이너(RANDOM_PORT)와 지원자 저장소의 설정 그대로 띄우고
 * 공개 계약(HTTP)으로만 확인한다. 케이스(hidden/cases)는 이 클래스를 상속한다.
 *
 * <p><b>운영 설정 덮어쓰기</b>: 채점 조절값 중 운영 조건(풀 크기·획득 대기·요청 스레드)은 {@link #OPERATING} 의 Spring 속성으로
 * 넣는다. 지원자 저장소의 application.yml 보다 우선하고, 지원자가 코드에서 직접 설정한 값보다는 약하다
 * (손 예시 r2b 의 환경변수 주입과 같은 우선순위). 이 표는 manifest {@code harness_params[].config_keys} 와 같아야 한다
 * — tests/starter/test_templates.py 가 대조한다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Import(GraderJdbcHarness.class)
public abstract class GraderSupport {

    /** 채점 조절값 이름 → Spring 속성. */
    public static final Map<String, String> OPERATING = operating();

    protected static final ObjectMapper JSON = new ObjectMapper();
    protected static final HttpClient HTTP = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(5))
            .build();
    /**
     * 요청 하나 한도. 정하지 않으면 30초와 (연결 대기 한도 × 2 + 5초) 중 긴 쪽 — 연결을 못 얻은 요청이 앱의 오류 응답으로
     * 끝나는 것을 보려면 클라이언트가 그보다 오래 기다려야 한다 (연결 대기 한도 30초 과제에서 두 한도가 같아 경합했다).
     */
    protected static final Duration REQUEST_TIMEOUT = requestTimeout();
    /**
     * HTTP 클라이언트의 요청 하나 한도 — 측정 한도(REQUEST_TIMEOUT)는 Burst 의 마감이 잰다. 클라이언트까지 짧으면 측정하지 않는
     * 요청(데우기·조회 확인)도 기동 직후 지연으로 예외가 나 올바른 구현이 떨어진다 (2026-11-10 목록 과제: request_timeout_s=1
     * 에서 데우기 요청이 HttpTimeoutException — 바쁜 기계에서만 나는 불안정한 판정).
     */
    static final Duration CLIENT_TIMEOUT =
            REQUEST_TIMEOUT.compareTo(Duration.ofSeconds(30)) < 0 ? Duration.ofSeconds(30) : REQUEST_TIMEOUT;
    /** 비동기(이벤트·아웃박스) 설계도 공정하게 보려고 결과가 나타날 때까지 기다리는 최대 시간. */
    protected static final Duration AWAIT_TIMEOUT =
            Duration.ofMillis(GraderParams.longValue("await_timeout_ms", 5000));

    @LocalServerPort
    protected int port;

    static Duration requestTimeout() {
        if (GraderParams.raw("request_timeout_s").isPresent()) {
            return Duration.ofSeconds(GraderParams.longValue("request_timeout_s", 30));
        }
        long poolWaitMs = GraderParams.longValue("connection_timeout_ms", 0);
        return Duration.ofMillis(Math.max(30_000, poolWaitMs * 2 + 5_000));
    }

    private static Map<String, String> operating() {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("pool_size", "spring.datasource.hikari.maximum-pool-size");
        m.put("connection_timeout_ms", "spring.datasource.hikari.connection-timeout");
        m.put("server_threads", "server.tomcat.threads.max");
        return m;
    }

    @DynamicPropertySource
    static void operatingConditions(DynamicPropertyRegistry registry) {
        OPERATING.forEach((param, property) ->
                GraderParams.raw(param).ifPresent(value -> registry.add(property, () -> value)));
    }

    @BeforeEach
    void requireHarness() {
        if (GraderJdbcHarness.WRAPPED_DATASOURCES.get() <= 0) {
            throw new HarnessException("채점 하네스가 DataSource 빈을 감싸지 못함 — 지연·주입 없이 돌면 판정이 흔들리므로 중단");
        }
    }

    protected URI uri(String path) {
        return URI.create("http://localhost:" + port + path);
    }

    /** 공개 계약대로 요청 하나. body 가 null 이면 본문 없음. */
    protected HttpResponse<String> send(String method, String path, Map<String, String> headers, String jsonBody)
            throws IOException, InterruptedException {
        HttpRequest.Builder b = HttpRequest.newBuilder(uri(path)).timeout(CLIENT_TIMEOUT);
        if (headers != null) {
            headers.forEach(b::header);
        }
        if (jsonBody != null) {
            b.header("Content-Type", "application/json");
        }
        b.method(method, jsonBody == null ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(jsonBody));
        return HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    protected HttpResponse<String> get(String path) throws IOException, InterruptedException {
        return send("GET", path, null, null);
    }

    protected <T> T json(String body, TypeReference<T> type) throws IOException {
        return JSON.readValue(body, type);
    }

    protected Map<String, Object> jsonObject(String body) throws IOException {
        return JSON.readValue(body, new TypeReference<>() { });
    }

    /** done 이 참이 될 때까지(최대 {@link #AWAIT_TIMEOUT}) poll 을 다시 부른다. 마지막 값을 돌려준다. */
    protected <T> T await(Callable<T> poll, Predicate<T> done) throws Exception {
        Instant deadline = Instant.now().plus(AWAIT_TIMEOUT);
        T value = poll.call();
        while (!done.test(value) && Instant.now().isBefore(deadline)) {
            Thread.sleep(50);
            value = poll.call();
        }
        return value;
    }

    /** 시각 문자열을 비교용으로 읽는다. 알 수 없는 형식이면 null. */
    protected static Instant parseTime(Object value) {
        if (value == null) {
            return null;
        }
        String s = value.toString();
        try {
            return OffsetDateTime.parse(s).toInstant();
        } catch (RuntimeException ignored) {
            // 다음 형식
        }
        try {
            return Instant.parse(s);
        } catch (RuntimeException ignored) {
            // 다음 형식
        }
        try {
            return LocalDateTime.parse(s).toInstant(ZoneOffset.UTC);
        } catch (RuntimeException ignored) {
            return null;
        }
    }

    /** 진단 한 줄을 표준 출력으로 (판정에는 쓰지 않는다). */
    protected static void diag(String format, Object... args) {
        System.out.printf("[grader] " + format + "%n", args);
    }
}
