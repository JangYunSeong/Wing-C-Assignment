package com.example.service.grading;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

import javax.sql.DataSource;

import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.datasource.DelegatingDataSource;

/**
 * 채점 전용 JDBC 하네스 (잠긴 하네스 — 과제와 무관). 지원자 코드·설정은 건드리지 않고 컨텍스트의 모든 DataSource 빈을 감싼다.
 * 손 예시(git 밖, Java)의 하네스를 과제와 무관하게 일반화했다.
 *
 * <ul>
 *   <li><b>지연</b> {@link #queryDelayMs}: SQL 문 실행마다 지연을 더한다. H2 인메모리는 문 하나가 수 μs 라 동시 요청이
 *       실제로 겹치지 않을 수 있다 — 실제 DB 왕복을 흉내 내 겹치게 만든다. 설계와 무관하게 모든 SQL 에 똑같이 적용된다.</li>
 *   <li><b>실행 오류 주입</b> {@link #failStatements}: 정규식에 맞는 SQL 문 실행을 실패시킨다.</li>
 *   <li><b>커밋 오류 주입</b> {@link #failCommitAfter}: 정규식에 맞는 SQL 을 실행한 연결의 다음 commit 을 실패시킨다
 *       — "검사는 통과했지만 커밋 단계에서 롤백된" 변경을 만든다.</li>
 *   <li><b>진단</b>: 스레드(=요청) 하나가 동시에 요구한 커넥션 수의 최댓값(보유 + 대기)과 획득 횟수. 판정에는 쓰지 않는다.</li>
 *   <li><b>케이스 사이 정리</b> {@link #settled}: 감싼 DataSource 마다 지금 쥐었거나 기다리는 연결 수를 센다 —
 *       {@link GraderSettle} 가 케이스가 끝날 때 이 수가 0 이 되기를 기다린다.</li>
 * </ul>
 */
@TestConfiguration(proxyBeanMethods = false)
public class GraderJdbcHarness {

    public static volatile long queryDelayMs = 0;
    public static volatile Pattern failStatements = null;
    public static volatile Pattern failCommitAfter = null;

    public static final AtomicInteger MAX_DEMAND_PER_THREAD = new AtomicInteger();
    public static final AtomicInteger ACQUISITIONS = new AtomicInteger();
    public static final AtomicInteger WRAPPED_DATASOURCES = new AtomicInteger();
    public static final AtomicInteger INJECTED_FAILURES = new AtomicInteger();

    private static final ThreadLocal<AtomicInteger> DEMAND = ThreadLocal.withInitial(AtomicInteger::new);

    public static void resetStats() {
        MAX_DEMAND_PER_THREAD.set(0);
        ACQUISITIONS.set(0);
        INJECTED_FAILURES.set(0);
    }

    /** 지연·오류 주입을 모두 끈다. 케이스는 finally 에서 부른다. */
    public static void reset() {
        queryDelayMs = 0;
        failStatements = null;
        failCommitAfter = null;
    }

    @Bean
    static BeanPostProcessor graderDataSourceWrapper() {
        return new BeanPostProcessor() {
            @Override
            public Object postProcessAfterInitialization(Object bean, String beanName) {
                if (bean instanceof DataSource ds && !(bean instanceof GraderDataSource)) {
                    WRAPPED_DATASOURCES.incrementAndGet();
                    return new GraderDataSource(ds);
                }
                return bean;
            }
        };
    }

    static final class GraderDataSource extends DelegatingDataSource {

        // 이 DataSource 에서 지금 쥐었거나 기다리는 연결 수 — 컨텍스트마다 따로 센다 (닫은 컨텍스트에 남은 요청을 세지 않게)
        final AtomicInteger inFlight = new AtomicInteger();

        GraderDataSource(DataSource target) {
            super(target);
        }

        @Override
        public Connection getConnection() throws SQLException {
            return track(inFlight, () -> obtainTargetDataSource().getConnection());
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            return track(inFlight, () -> obtainTargetDataSource().getConnection(username, password));
        }
    }

    /**
     * 이 컨텍스트의 감싼 DataSource 가 모두 비었나 — 쥐었거나 기다리는 연결이 50ms 간격으로 연속 3번 0 이면 참, {@code cap} 을 넘기면 거짓.
     * 2026-11-26 교착 재료: 흔한 구현이 hc1 버스트에서 교착한 요청이 서버에 남아(버스트는 클라이언트 쪽에서만 포기한다) 다음 케이스 hc2
     * 의 요청 하나가 줄을 서다 시간 초과 — 의도한 사유(rollback_left_success) 대신 테스트 예외가 됐다 (hc2 만 돌리면 의도한 사유).
     */
    static boolean settled(ApplicationContext context, Duration cap) throws InterruptedException {
        List<GraderDataSource> sources = context.getBeansOfType(DataSource.class).values().stream()
                .filter(GraderDataSource.class::isInstance)
                .map(GraderDataSource.class::cast)
                .toList();
        long deadline = System.nanoTime() + cap.toNanos();
        int quiet = 0;
        while (true) {
            quiet = sources.stream().allMatch(s -> s.inFlight.get() == 0) ? quiet + 1 : 0;
            if (quiet >= 3) {
                return true;
            }
            if (System.nanoTime() > deadline) {
                return false;
            }
            Thread.sleep(50);
        }
    }

    private interface SqlSupplier<T> {
        T get() throws SQLException;
    }

    private static Connection track(AtomicInteger inFlight, SqlSupplier<Connection> acquire) throws SQLException {
        AtomicInteger demand = DEMAND.get();
        MAX_DEMAND_PER_THREAD.accumulateAndGet(demand.incrementAndGet(), Math::max);
        inFlight.incrementAndGet();
        Connection target;
        try {
            target = acquire.get();
        } catch (SQLException | RuntimeException e) {
            demand.decrementAndGet();
            inFlight.decrementAndGet();
            throw e;
        }
        ACQUISITIONS.incrementAndGet();
        AtomicBoolean closed = new AtomicBoolean();
        return wrapConnection(target, () -> {
            if (closed.compareAndSet(false, true)) {
                demand.decrementAndGet();
                inFlight.decrementAndGet();
            }
        });
    }

    private static Connection wrapConnection(Connection target, Runnable onClose) {
        AtomicBoolean commitArmed = new AtomicBoolean();
        return (Connection) Proxy.newProxyInstance(GraderJdbcHarness.class.getClassLoader(),
                new Class<?>[]{Connection.class}, (proxy, method, args) -> {
                    switch (method.getName()) {
                        case "equals":
                            return proxy == args[0];
                        case "hashCode":
                            return System.identityHashCode(proxy);
                        case "close":
                            onClose.run();
                            break;
                        case "commit":
                            if (commitArmed.getAndSet(false) && failCommitAfter != null) {
                                INJECTED_FAILURES.incrementAndGet();
                                // 실제 DB 처럼 실패한 커밋의 트랜잭션은 버린다 — 열어 두면 프레임워크가 정리하며 자동 커밋을 다시 켤 때
                                // (setAutoCommit(true) — JDBC 규약상 열린 트랜잭션을 커밋) 변경이 남는다 (2026-11-08 알파)
                                target.rollback();
                                throw new SQLException("grader: injected commit-time failure");
                            }
                            break;
                        case "rollback":
                            commitArmed.set(false);
                            break;
                        default:
                            break;
                    }
                    Object result = invoke(target, method, args);
                    if (result instanceof Statement st && !method.getName().equals("unwrap")) {
                        String sql = args != null && args.length > 0 && args[0] instanceof String s ? s : null;
                        return wrapStatement(st, sql, (Connection) proxy, commitArmed);
                    }
                    return result;
                });
    }

    private static Statement wrapStatement(Statement target, String preparedSql, Connection owner,
                                           AtomicBoolean commitArmed) {
        Class<?> type = target instanceof CallableStatement ? CallableStatement.class
                : target instanceof PreparedStatement ? PreparedStatement.class : Statement.class;
        return (Statement) Proxy.newProxyInstance(GraderJdbcHarness.class.getClassLoader(),
                new Class<?>[]{type}, (proxy, method, args) -> {
                    String name = method.getName();
                    if (name.equals("equals")) {
                        return proxy == args[0];
                    }
                    if (name.equals("hashCode")) {
                        return System.identityHashCode(proxy);
                    }
                    if (name.equals("getConnection")) {
                        return owner;
                    }
                    if (name.startsWith("execute")) {
                        String sql = preparedSql != null ? preparedSql
                                : args != null && args.length > 0 && args[0] instanceof String s ? s : "";
                        Pattern fail = failStatements;
                        if (fail != null && fail.matcher(sql).find()) {
                            INJECTED_FAILURES.incrementAndGet();
                            throw new SQLException("grader: injected statement failure");
                        }
                        Pattern arm = failCommitAfter;
                        if (arm != null && arm.matcher(sql).find()) {
                            commitArmed.set(true);
                        }
                        pause();
                    }
                    return invoke(target, method, args);
                });
    }

    private static void pause() throws SQLException {
        long ms = queryDelayMs;
        if (ms <= 0) {
            return;
        }
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new SQLException("grader: interrupted during injected latency", e);
        }
    }

    private static Object invoke(Object target, Method method, Object[] args) throws Throwable {
        try {
            return method.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
