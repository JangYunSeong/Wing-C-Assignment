package com.example.service.grading;

/**
 * 하네스 자체가 제대로 붙지 않았을 때 던진다 (잠긴 하네스). 실행기는 이 예외 이름으로 결과를 {@code HARNESS-ERR} 로 분류한다
 * — 지원자 구현의 실패({@code ASSERT})와 섞지 않는다 (spec-to-code §4.1 결과 분류).
 */
public class HarnessException extends RuntimeException {

    public HarnessException(String message) {
        super(message);
    }

    public HarnessException(String message, Throwable cause) {
        super(message, cause);
    }
}
