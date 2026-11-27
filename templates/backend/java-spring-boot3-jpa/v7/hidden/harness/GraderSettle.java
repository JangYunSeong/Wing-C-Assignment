package com.example.service.grading;

import java.time.Duration;

import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.TestContext;
import org.springframework.test.context.support.AbstractTestExecutionListener;
import org.springframework.test.context.support.DependencyInjectionTestExecutionListener;

/**
 * 케이스 사이 정리 (잠긴 하네스 — 과제와 무관). 테스트 메서드가 끝나면 서버가 빌 때까지(감싼 DataSource 의 쥐었거나 기다리는 연결 0)
 * {@link #SETTLE} 동안 기다린다. 그래도 비지 않으면 그 앱 컨텍스트를 버려, 다음 테스트는 새 앱(새 DB·풀·서버)에서 돈다.
 *
 * <p>판정은 바꾸지 않는다 — 방금 끝난 케이스의 결과는 그대로이고, 다음 케이스가 앞 케이스가 남긴 요청에 막히지 않게 할 뿐이다.
 * 버스트는 클라이언트 쪽에서만 포기해서 교착한 요청은 서버에 남고, 연결 대기 한도(기본 30초)마다 풀 크기씩만 풀린다 — 다 빠지길
 * 기다리면 수 분이라 짧게 기다리고 새 앱으로 넘긴다.
 */
public class GraderSettle extends AbstractTestExecutionListener {

    static final Duration SETTLE = Duration.ofSeconds(10);

    @Override
    public void afterTestMethod(TestContext testContext) throws Exception {
        if (!testContext.hasApplicationContext()) {
            return;
        }
        if (!GraderJdbcHarness.settled(testContext.getApplicationContext(), SETTLE)) {
            testContext.markApplicationContextDirty(DirtiesContext.HierarchyMode.EXHAUSTIVE);
            testContext.setAttribute(DependencyInjectionTestExecutionListener.REINJECT_DEPENDENCIES_ATTRIBUTE, Boolean.TRUE);
        }
    }
}
