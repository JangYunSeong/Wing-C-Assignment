package com.example.service.grading;

/**
 * 의도한 실패 사유 표시 (잠긴 하네스). 케이스는 단언 설명에 {@code Reasons.tag("rollback_left_success")} 를 붙인다.
 * verifier 는 JUnit 보고서의 실패 메시지에서 {@code [reason:<code>]} 를 찾아 manifest {@code failure_reasons} 와 대조한다 (게이트 ③).
 * 코드는 manifest 목록에 있는 것만 쓴다 — 계약 검사(starter/contract.py)가 HiddenPlan 단계에서 막는다.
 */
public final class Reasons {

    private Reasons() {
    }

    public static String tag(String code) {
        return "[reason:" + code + "]";
    }

    /** 단언 설명 앞에 사유 표시를 붙인다. 예: {@code assertThat(x).as(Reasons.as("record_missing", "요청 %d", id))}. */
    public static String as(String code, String description, Object... args) {
        return tag(code) + " " + String.format(description, args);
    }
}
