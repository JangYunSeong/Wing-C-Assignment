package com.example.service.grading.selftest;

import java.sql.SQLException;
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

    @Test
    void 하네스_예외_이름이_분류에_쓰인다() {
        assertThat(new HarnessException("x")).isInstanceOf(RuntimeException.class);
        assertThat(HarnessException.class.getSimpleName()).isEqualTo("HarnessException");
    }
}
