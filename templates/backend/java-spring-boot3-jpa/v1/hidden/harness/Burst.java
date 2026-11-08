package com.example.service.grading;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.IntUnaryOperator;

/**
 * 동시 요청 실행기 (잠긴 하네스). {@code concurrency} 건을 출발선(barrier)에서 한꺼번에 보내고 결과를 모은다.
 * 손 예시(git 밖)의 버스트를 과제와 무관하게 일반화했다.
 */
public final class Burst {

    /** actual = HTTP 상태 코드, -1 = 클라이언트 시간 초과·연결 오류. */
    public record Outcome(int index, int expected, int actual, long millis, String error) {
        public boolean ok() {
            return actual == expected;
        }
    }

    @FunctionalInterface
    public interface Call {
        /** index 번째 요청을 보내고 HTTP 상태 코드를 돌려준다. */
        int send(int index) throws Exception;
    }

    /** 출발선 대기·스레드 시작 몫 — 요청 한도(wait)는 HTTP 클라이언트가 지키고, 이것은 그 결과를 모으는 쪽의 여유. */
    static final Duration GRACE = Duration.ofSeconds(2);

    private Burst() {
    }

    public static List<Outcome> fire(int concurrency, IntUnaryOperator expected, Call call, Duration wait)
            throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(concurrency);
        try {
            CyclicBarrier gate = new CyclicBarrier(concurrency);
            List<Future<Outcome>> futures = new ArrayList<>();
            for (int i = 0; i < concurrency; i++) {
                int index = i;
                futures.add(pool.submit(() -> {
                    gate.await();
                    long t0 = System.nanoTime();
                    int want = expected.applyAsInt(index);
                    try {
                        int status = call.send(index);
                        return new Outcome(index, want, status, (System.nanoTime() - t0) / 1_000_000, null);
                    } catch (Exception e) {
                        return new Outcome(index, want, -1, (System.nanoTime() - t0) / 1_000_000, e.toString());
                    }
                }));
            }
            // 버스트 전체에 마감 하나 — 요청마다 wait 를 새로 주면 앞 요청을 기다린 시간만큼 뒤 요청 한도가 늘어난다.
            // 출발선 대기·스레드 시작 몫으로 GRACE 를 더하고, 넘기면 예외 대신 -1 로 남긴다 (2026-11-07 알파 테스트:
            // 연결 대기 한도 30초 과제에서 교착된 요청을 기다리다 TimeoutException 이 나 의도한 실패가 테스트 자체 오류로 분류됨).
            long deadline = System.nanoTime() + wait.toNanos() + GRACE.toNanos();
            List<Outcome> outcomes = new ArrayList<>();
            for (int i = 0; i < futures.size(); i++) {
                Future<Outcome> f = futures.get(i);
                long left = Math.max(0, deadline - System.nanoTime());
                try {
                    outcomes.add(f.get(left, TimeUnit.NANOSECONDS));
                } catch (TimeoutException | ExecutionException e) {
                    f.cancel(true);
                    outcomes.add(new Outcome(i, expected.applyAsInt(i), -1, wait.toMillis() + GRACE.toMillis(),
                            "grader wait: " + e));
                }
            }
            return outcomes;
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * 운영 피크를 버스트로 재현 — {@code concurrency} 건씩 한꺼번에, {@code rounds} 번 차례로. 한 번에 {@code concurrency × rounds} 건을
     * 보내지 않는다 (2026-11-07: 숨은 테스트가 96건을 한꺼번에 보내 허용 설계도 시간 초과). index 는 회차를 이어 0 부터
     * {@code concurrency × rounds - 1} 까지 — 버스트마다 다른 행을 쓰려면 {@code firstId + index}.
     */
    public static List<Outcome> rounds(int concurrency, int rounds, IntUnaryOperator expected, Call call, Duration wait)
            throws Exception {
        List<Outcome> all = new ArrayList<>();
        for (int r = 0; r < rounds; r++) {
            int base = r * concurrency;
            for (Outcome o : fire(concurrency, i -> expected.applyAsInt(base + i), i -> call.send(base + i), wait)) {
                all.add(new Outcome(base + o.index(), o.expected(), o.actual(), o.millis(), o.error()));
            }
        }
        return all;
    }

    /** 진단 한 줄 — {@code [grader] <label> burst <n>: status={200=16, 409=16} wrong=0 max_latency_ms=412}. */
    public static String summary(String label, int n, List<Outcome> outcomes) {
        Map<Integer, Integer> byStatus = new TreeMap<>();
        outcomes.forEach(o -> byStatus.merge(o.actual(), 1, Integer::sum));
        long maxMs = outcomes.stream().mapToLong(Outcome::millis).max().orElse(0);
        long wrong = outcomes.stream().filter(o -> !o.ok()).count();
        return String.format("[grader] %s burst %d: status=%s wrong=%d max_latency_ms=%d",
                label, n, byStatus, wrong, maxMs);
    }

    /** 기대와 다른 응답 수 (모든 종류) — 의도한 사유를 붙이지 않는다: 500·시간 초과·규칙 불일치가 섞여 어떤 실패든 같은 사유로 보인다. */
    public static long wrong(List<Outcome> outcomes) {
        return outcomes.stream().filter(o -> !o.ok()).count();
    }

    /**
     * 시간 초과(-1)·서버 오류(5xx) 수 — 연결 고갈·교착 같은 운영 조건 실패. 운영 조건 사유(예: pool_acquire_timeout)는 이 수에 붙인다.
     * 2026-11-08 숨은 테스트 검토: 14개 케이스 모두 사유를 wrong(...) 에 붙여 원인과 상관없이 의도한 사유로 분류됐다.
     */
    public static long failures(List<Outcome> outcomes) {
        return outcomes.stream().filter(o -> o.actual() == -1 || o.actual() >= 500).count();
    }

    /** 그 밖의 기대와 다른 응답 수 (예: 200 기대에 400·404) — 규칙·계약 불일치. 운영 조건 사유를 붙이지 않는다. */
    public static long mismatches(List<Outcome> outcomes) {
        return outcomes.stream().filter(o -> !o.ok() && o.actual() != -1 && o.actual() < 500).count();
    }
}
