package com.example.service.grading;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.Properties;

/**
 * 채점 조절값 (잠긴 하네스). 빌드 파이프라인이 HiddenPlan.harness 를 {@code grader-params.properties} 로 써서
 * 테스트 클래스패스에 넣는다 (manifest {@code hidden_layout.params}). 키 이름 = manifest {@code harness_params[].name}.
 *
 * <p>같은 이름의 시스템 속성 {@code grader.<name>} 이나 환경변수 {@code GRADER_<NAME>} 이 있으면 그것이 이긴다
 * (재현 실험용 — 예: 지연 0 으로 다시 돌리기).
 */
public final class GraderParams {

    public static final String RESOURCE = "/grader-params.properties";

    private static final Properties FILE = load();

    private GraderParams() {
    }

    private static Properties load() {
        Properties p = new Properties();
        try (InputStream in = GraderParams.class.getResourceAsStream(RESOURCE)) {
            if (in != null) {
                p.load(in);
            }
        } catch (IOException e) {
            throw new HarnessException("채점 조절값 파일을 읽지 못함: " + RESOURCE, e);
        }
        return p;
    }

    /** 값 원문. 시스템 속성 → 환경변수 → 파일 순. */
    public static Optional<String> raw(String name) {
        String v = System.getProperty("grader." + name);
        if (v == null) {
            v = System.getenv("GRADER_" + name.toUpperCase(Locale.ROOT));
        }
        if (v == null) {
            v = FILE.getProperty(name);
        }
        return Optional.ofNullable(v).map(String::trim).filter(s -> !s.isEmpty());
    }

    public static boolean has(String name) {
        return raw(name).isPresent();
    }

    public static long longValue(String name, long fallback) {
        return raw(name).map(s -> parse(name, s)).orElse(fallback);
    }

    public static int intValue(String name, int fallback) {
        return Math.toIntExact(longValue(name, fallback));
    }

    /** 쉼표로 구분한 정수 목록 (manifest kind=int_list). 없으면 빈 목록. */
    public static List<Long> longList(String name) {
        List<Long> out = new ArrayList<>();
        raw(name).ifPresent(s -> {
            for (String part : s.split(",")) {
                if (!part.isBlank()) {
                    out.add(parse(name, part.trim()));
                }
            }
        });
        return out;
    }

    private static long parse(String name, String value) {
        try {
            return Long.parseLong(value.replace("_", ""));
        } catch (NumberFormatException e) {
            throw new HarnessException("채점 조절값 " + name + " 이 정수가 아님: " + value, e);
        }
    }
}
