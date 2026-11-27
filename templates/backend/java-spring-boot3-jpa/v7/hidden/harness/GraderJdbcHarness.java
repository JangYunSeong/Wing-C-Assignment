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
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

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
    // 요청당 DB 사용 세기 (v7, #339 — 실패 사유 db_per_request). 측정 구간 전체의 SQL 문을 모은다 — 요청을 처리한 스레드만 보면
    // 비동기로 넘긴 DB 작업(@Async·CompletableFuture)을 놓친다 (#339 0단계: 비동기 쓰기 변형을 0 으로 셈)
    // 세는 DataSource — 세기를 시작한 앱(컨텍스트)의 것만. 연결이 비지 않아 버린 앱에 남은 요청의 문은 세지 않는다
    private static volatile boolean counting = false;
    private static volatile Set<GraderDataSource> countingSources = Set.of();
    private static final List<String> COUNTED = Collections.synchronizedList(new ArrayList<>());
    static final Duration COUNT_GRACE = Duration.ofMillis(300); // 비동기로 넘긴 작업이 연결을 잡을 때까지
    static final Duration COUNT_DRAIN = Duration.ofSeconds(5);
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
        counting = false;
        countingSources = Set.of();
        failStatements = null;
        failCommitAfter = null;
    }

    /**
     * 세기 시작 — 이 뒤로 context 의 감싼 DataSource 에서 실행되는 SQL 문을 모은다 (요청 스레드든 비동기 작업이든). 다른 컨텍스트의 문은
     * 세지 않는다. 케이스는 GraderSupport.countStatements(…) 로 쓴다.
     */
    static void startCounting(ApplicationContext context) {
        Set<GraderDataSource> sources = Collections.newSetFromMap(new IdentityHashMap<>());
        sources.addAll(sourcesOf(context));
        synchronized (COUNTED) {
            COUNTED.clear();
        }
        countingSources = sources;
        counting = true;
    }

    /**
     * 세기 끝 — 비동기로 넘긴 DB 작업까지 세도록 잠깐 기다린 뒤 연결이 빌 때까지(최대 COUNT_DRAIN) 기다리고, 모은 SQL 문을 돌려준다.
     * 끝내 비지 않아도 모은 것을 돌려준다 — 요청을 처리하며 실행한 문은 이미 다 셌고, 놓치는 것은 COUNT_DRAIN 뒤에도 남은 비동기 작업뿐이다.
     */
    static List<String> drainAndStop(ApplicationContext context) throws InterruptedException {
        Thread.sleep(COUNT_GRACE.toMillis());
        settled(context, COUNT_DRAIN);
        counting = false;
        synchronized (COUNTED) {
            return new ArrayList<>(COUNTED);
        }
    }

    static void stopCounting() {
        counting = false;
        countingSources = Set.of();
    }

    /**
     * 표(tables)를 하나도 건드리지 않은 SQL 문 수 — 표 이름은 표 자리(from·join·update·into 뒤, 스키마 접두어·따옴표 허용)에서만, 대소문자
     * 없이 대조한다. 같은 이름의 칼럼(세션 조회의 s1_0.status 와 업무 표 status)은 업무 표로 보지 않는다 — 낱말로만 대조하면 흔한 구현의 세션
     * 조회가 업무 문으로 빠져 세기가 적게 나왔다 (#339 검토). 쉼표 조인(from a, b)의 뒤 표는 보지 않는다 (Hibernate 6 은 쓰지 않음).
     */
    public static long statementsNotTouching(List<String> statements, String... tables) {
        if (tables.length == 0) {
            return statements.size();
        }
        Pattern any = Pattern.compile(
                "\\b(?:from|join|update|into)\\s+(?:[\"`]?\\w+[\"`]?\\.)?[\"`]?(?:"
                        + Arrays.stream(tables).map(Pattern::quote).collect(Collectors.joining("|"))
                        + ")[\"`]?(?!\\w)",
                Pattern.CASE_INSENSITIVE);
        return statements.stream().filter(sql -> !any.matcher(sql).find()).count();
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
            return track(this, () -> obtainTargetDataSource().getConnection());
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            return track(this, () -> obtainTargetDataSource().getConnection(username, password));
        }
    }

    /**
     * 이 컨텍스트의 감싼 DataSource 가 모두 비었나 — 쥐었거나 기다리는 연결이 50ms 간격으로 연속 3번 0 이면 참, {@code cap} 을 넘기면 거짓.
     * 2026-11-26 교착 재료: 흔한 구현이 hc1 버스트에서 교착한 요청이 서버에 남아(버스트는 클라이언트 쪽에서만 포기한다) 다음 케이스 hc2
     * 의 요청 하나가 줄을 서다 시간 초과 — 의도한 사유(rollback_left_success) 대신 테스트 예외가 됐다 (hc2 만 돌리면 의도한 사유).
     */
    static boolean settled(ApplicationContext context, Duration cap) throws InterruptedException {
        List<GraderDataSource> sources = sourcesOf(context);
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

    private static List<GraderDataSource> sourcesOf(ApplicationContext context) {
        return context.getBeansOfType(DataSource.class).values().stream()
                .filter(GraderDataSource.class::isInstance)
                .map(GraderDataSource.class::cast)
                .toList();
    }

    private interface SqlSupplier<T> {
        T get() throws SQLException;
    }

    private static Connection track(GraderDataSource source, SqlSupplier<Connection> acquire) throws SQLException {
        AtomicInteger inFlight = source.inFlight;
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
        return wrapConnection(target, source, () -> {
            if (closed.compareAndSet(false, true)) {
                demand.decrementAndGet();
                inFlight.decrementAndGet();
            }
        });
    }

    private static Connection wrapConnection(Connection target, GraderDataSource source, Runnable onClose) {
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
                        return wrapStatement(st, sql, (Connection) proxy, commitArmed, source);
                    }
                    return result;
                });
    }

    private static Statement wrapStatement(Statement target, String preparedSql, Connection owner,
                                           AtomicBoolean commitArmed, GraderDataSource source) {
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
                        if (counting && !sql.isBlank() && countingSources.contains(source)) {
                            COUNTED.add(sql);
                        }
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
