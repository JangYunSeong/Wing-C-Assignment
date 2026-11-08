package com.example.service.grading.selftest;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

import com.example.service.grading.Burst;
import com.example.service.grading.GraderJdbcHarness;
import com.example.service.grading.GraderParams;
import com.example.service.grading.GraderSupport;
import com.example.service.grading.HarnessException;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.env.Environment;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import javax.sql.DataSource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 하네스 자체 점검 (잠긴 하네스). verifier 가 케이스보다 먼저 돌린다 — 여기서 실패하면 결과는 {@code HARNESS-ERR} 이고
 * 지원자·참조 구현의 판정을 내지 않는다. 과제 코드와 무관하게 템플릿 뼈대만으로 통과해야 한다.
 */
class HarnessSelfTest extends GraderSupport {

    @Autowired
    Environment env;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    DataSource dataSource;

    @AfterEach
    void off() {
        GraderJdbcHarness.reset();
    }

    @Test
    void 버스트는_동시_요청_수씩_차례로() throws Exception {
        AtomicInteger inFlight = new AtomicInteger();
        AtomicInteger maxInFlight = new AtomicInteger();
        List<Burst.Outcome> outcomes = Burst.rounds(4, 3, i -> 200, i -> {
            maxInFlight.accumulateAndGet(inFlight.incrementAndGet(), Math::max);
            Thread.sleep(30);
            inFlight.decrementAndGet();
            return 200;
        }, Duration.ofSeconds(10));
        assertThat(outcomes).hasSize(12);
        assertThat(outcomes.stream().map(Burst.Outcome::index).sorted().toList())
                .containsExactlyElementsOf(java.util.stream.IntStream.range(0, 12).boxed().toList());
        assertThat(maxInFlight.get()).isLessThanOrEqualTo(4);
        assertThat(Burst.wrong(outcomes)).isZero();
    }

    @Test
    void 버스트가_시간을_넘긴_요청을_예외_대신_실패로_남긴다() throws Exception {
        List<Burst.Outcome> outcomes = Burst.fire(2, i -> 200, i -> {
            if (i == 1) {
                Thread.sleep(10_000);
            }
            return 200;
        }, Duration.ofMillis(100));
        assertThat(outcomes).hasSize(2);
        assertThat(outcomes.get(0).ok()).isTrue();
        assertThat(outcomes.get(1).actual()).isEqualTo(-1);
        assertThat(Burst.wrong(outcomes)).isEqualTo(1);
    }

    @Test
    void 버스트_결과를_운영_실패와_규칙_불일치로_나눠_센다() {
        List<Burst.Outcome> outcomes = List.of(
                new Burst.Outcome(0, 200, 200, 1, null),
                new Burst.Outcome(1, 200, 500, 1, null),
                new Burst.Outcome(2, 200, -1, 1, "timeout"),
                new Burst.Outcome(3, 200, 400, 1, null),
                new Burst.Outcome(4, 400, 400, 1, null));
        assertThat(Burst.wrong(outcomes)).isEqualTo(3);
        assertThat(Burst.failures(outcomes)).isEqualTo(2);
        assertThat(Burst.mismatches(outcomes)).isEqualTo(1);
    }

    @Test
    void 데이터소스를_감쌌다() {
        assertThat(GraderJdbcHarness.WRAPPED_DATASOURCES.get()).isPositive();
    }

    @Test
    void 운영_조건이_spring_속성으로_들어갔다() {
        OPERATING.forEach((param, property) -> GraderParams.raw(param).ifPresent(value ->
                assertThat(env.getProperty(property)).as("%s → %s", param, property).isEqualTo(value)));
    }

    @Test
    void 지연이_sql_마다_더해진다() {
        GraderJdbcHarness.queryDelayMs = 60;
        long t0 = System.nanoTime();
        jdbc.queryForObject("SELECT 1", Integer.class);
        long ms = (System.nanoTime() - t0) / 1_000_000;
        assertThat(ms).as("지연 60ms 를 켠 SELECT 1").isGreaterThanOrEqualTo(50);
    }

    @Test
    void 실행_오류를_주입한다() {
        GraderJdbcHarness.failStatements = Pattern.compile("SELECT 2");
        assertThatThrownBy(() -> jdbc.queryForObject("SELECT 2", Integer.class))
                .hasRootCauseInstanceOf(SQLException.class)
                .hasStackTraceContaining("injected statement failure");
        assertThat(jdbc.queryForObject("SELECT 1", Integer.class)).isEqualTo(1);
    }

    @Test
    void 커밋_오류를_주입한다() {
        GraderJdbcHarness.failCommitAfter = Pattern.compile("SELECT 3");
        TransactionTemplate tx = new TransactionTemplate(txManager);
        assertThatThrownBy(() -> tx.executeWithoutResult(s -> jdbc.queryForObject("SELECT 3", Integer.class)))
                .hasStackTraceContaining("injected commit-time failure");
        // 맞지 않는 SQL 만 쓴 트랜잭션은 그대로 커밋된다
        tx.executeWithoutResult(s -> jdbc.queryForObject("SELECT 1", Integer.class));
    }

    /**
     * 실제 DB 는 커밋이 실패하면 트랜잭션을 버린다. 주입한 커밋 실패도 그래야 한다 — 하네스가 예외만 던지고 트랜잭션을 연결에 열어 두면
     * 프레임워크가 정리하며 자동 커밋을 다시 켤 때(setAutoCommit(true) — JDBC 규약상 열린 트랜잭션을 커밋) 변경이 남는다.
     * 2026-11-08 알파: 이 때문에 허용 설계도 '롤백된 변경에 성공 기록' 케이스에서 흔한 구현과 똑같이 실패했다.
     */
    @Test
    void 커밋_오류를_주입하면_변경이_남지_않는다() throws Exception {
        jdbc.execute("CREATE TABLE IF NOT EXISTS grader_selftest_rollback (id INT PRIMARY KEY)");
        jdbc.update("DELETE FROM grader_selftest_rollback");
        GraderJdbcHarness.failCommitAfter = Pattern.compile("(?i)^\\s*insert\\s+into\\s+grader_selftest_rollback\\b");
        // 프레임워크 경로마다 다르게 정리하므로(롤백을 부르기도, 자동 커밋만 다시 켜기도) JDBC 수준에서 약속 자체를 본다
        try (Connection c = dataSource.getConnection()) {
            c.setAutoCommit(false);
            try (Statement st = c.createStatement()) {
                st.executeUpdate("INSERT INTO grader_selftest_rollback VALUES (1)");
            }
            assertThatThrownBy(c::commit).isInstanceOf(SQLException.class)
                    .hasMessageContaining("injected commit-time failure");
            c.setAutoCommit(true);
        }
        GraderJdbcHarness.reset();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM grader_selftest_rollback", Integer.class))
                .as("주입한 커밋 실패 뒤 변경이 남음 — 하네스가 트랜잭션을 버리지 않았다").isZero();
    }

    @Test
    void 하네스_예외_이름이_분류에_쓰인다() {
        assertThat(new HarnessException("x")).isInstanceOf(RuntimeException.class);
        assertThat(HarnessException.class.getSimpleName()).isEqualTo("HarnessException");
    }
}
