package com.example.service.grading.selftest;

import java.sql.SQLException;
import java.util.regex.Pattern;

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
