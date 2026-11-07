package com.example.service.grading;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
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
            List<Outcome> outcomes = new ArrayList<>();
            for (Future<Outcome> f : futures) {
                outcomes.add(f.get(wait.toMillis(), TimeUnit.MILLISECONDS));
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

    public static long wrong(List<Outcome> outcomes) {
        return outcomes.stream().filter(o -> !o.ok()).count();
    }
}
