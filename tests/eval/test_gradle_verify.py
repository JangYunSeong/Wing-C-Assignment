"""개발 서버 Gradle verifier 의 보고서 읽기 (eval/starter/gradle_verify.py) — 가상 보고서만, Gradle 은 돌리지 않는다."""

from __future__ import annotations

from pathlib import Path

from eval.starter.gradle_verify import case_ids, compile_errors, junit_results
from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import Overlay

SUITE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{cls}" tests="1" failures="{n}" errors="0">
  <testcase name="t" classname="{cls}" time="1.0">{body}</testcase>
</testsuite>
"""
CASES = "com.example.service.grading.cases"


def _xml(d: Path, cls: str, failure: tuple[str, str] | None = None) -> None:
    body = ""
    if failure is not None:
        body = f'<failure message="{failure[1]}" type="{failure[0]}">stack</failure>'
    (d / f"TEST-{cls}.xml").write_text(
        SUITE.format(cls=cls, n=int(failure is not None), body=body), "utf-8"
    )


def test_junit_results_classify_groups_and_failures(tmp_path: Path) -> None:
    _xml(tmp_path, "com.example.service.order.OrderControllerTest")
    _xml(tmp_path, "com.example.service.grading.HarnessSelfTest")
    _xml(
        tmp_path,
        f"{CASES}.PeakBurstTest",
        (
            "org.opentest4j.AssertionFailedError",
            "[[reason:pool_acquire_timeout] 기대와 다른 응답 수] expected: 0L but was: 32L",
        ),
    )
    _xml(
        tmp_path,
        f"{CASES}.FailRollbackTest",
        ("java.lang.IllegalStateException", "충분한 id 가 없음"),
    )
    hidden = Overlay(
        kind="hidden",
        files=(
            GeneratedFile(
                path="src/test/java/x/PeakBurstTest.java", content='@DisplayName("hc1 피크")'
            ),
            GeneratedFile(
                path="src/test/java/x/FailRollbackTest.java", content='@DisplayName("hc2")'
            ),
        ),
    )
    res = {
        (t.group, t.name): t for t in junit_results(tmp_path, case_ids(hidden, r"\bhc\d{1,2}\b"))
    }
    assert res[("existing", "OrderControllerTest")].result == "PASS"
    assert res[("selftest", "HarnessSelfTest")].result == "PASS"
    hc1 = res[("hidden", "hc1")]
    assert hc1.result == "ASSERT" and hc1.reason == "pool_acquire_timeout" and "32L" in hc1.message
    hc2 = res[("hidden", "hc2")]
    assert hc2.result == "HARNESS_ERR" and hc2.reason is None  # 단언이 아닌 예외 = 테스트 자체 문제


def test_compile_errors_are_relative_to_the_work_dir(tmp_path: Path) -> None:
    work = tmp_path / "r1-naive-all"
    log = (
        "> Task :compileTestJava FAILED\n"
        f"{work.resolve()}/src/test/java/com/x/Hc2Test.java:50: error: unreported exception IOException\n"
        "        send(...);\n"
        f"{work.resolve()}/src/test/java/com/x/Hc2Test.java:50: error: unreported exception IOException\n"
    )
    errs = compile_errors(log, work)
    assert [(e.path, e.line) for e in errs] == [("src/test/java/com/x/Hc2Test.java", 50)]


async def test_verify_runs_bare_starter_with_hidden_tests_only_when_starter_builds(
    tmp_path: Path,
) -> None:
    """2026-11-07 알파 테스트: 시작 코드 그대로 + 숨은 테스트를 돌려야 '이미 풀린 과제'를 잡는다 (repair._starter_solves)."""
    from eval.starter.gradle_verify import GradleVerifier
    from task_spec.schemas import TargetRun
    from task_spec.starter.templates import load_template

    def overlay(kind: str, label: str = "") -> Overlay:
        return Overlay(kind=kind, files=(GeneratedFile(path="x", content="x"),), label=label)  # type: ignore[arg-type]

    calls: list[tuple[str, str, int]] = []

    def fake(setup: str):  # type: ignore[no-untyped-def]
        def _target(target, kind, parts, hidden):  # type: ignore[no-untyped-def]
            calls.append((target, kind, len(parts)))
            return TargetRun(target=target, kind=kind, setup=setup if kind == "starter" else "ok")

        return _target

    v = GradleVerifier(load_template("backend/java-spring-boot3-jpa"), tmp_path)
    v._target = fake("ok")  # type: ignore[method-assign]
    rep = await v.verify(overlay("starter"), overlay("hidden"), [overlay("ref_naive", "naive-all")])
    assert calls == [
        ("starter", "starter", 1),
        ("starter+hidden", "starter_hidden", 2),
        ("naive-all", "ref_naive", 3),
    ]
    # 공개 테스트가 있으면 시작 코드·참조 구현 실행에 넣는다 (테스트 먼저 — 2026-11-08)
    calls.clear()
    await v.verify(
        overlay("starter"),
        overlay("hidden"),
        [overlay("ref_naive", "naive-all")],
        public=overlay("public_test"),
    )
    assert calls == [
        ("starter", "starter", 2),
        ("starter+hidden", "starter_hidden", 2),
        ("naive-all", "ref_naive", 4),
    ]
    assert [r.kind for r in rep.runs] == ["starter", "starter_hidden", "ref_naive"]
    calls.clear()
    v._target = fake("compile_error")  # type: ignore[method-assign]
    await v.verify(overlay("starter"), overlay("hidden"), [])
    assert [c[1] for c in calls] == [
        "starter"
    ]  # 시작 코드가 안 되면 숨은 테스트를 붙여 보지 않는다


def test_boot_cause_is_the_deepest_caused_by(tmp_path: Path) -> None:
    """2026-11-08 알파 4케이스: 수리 요청에 'boot_error' 한 단어만 가 같은 스키마 문법 오류를 되풀이 — 근본 원인 문장을 뽑는다."""
    from eval.starter.gradle_verify import boot_cause

    stack = (
        "java.lang.IllegalStateException: Failed to load ApplicationContext\n"
        "\tat org.springframework.Foo.bar(Foo.java:1)\n"
        "Caused by: org.springframework.beans.factory.BeanCreationException: Error creating bean\n"
        "Caused by: org.h2.jdbc.JdbcSQLSyntaxErrorException: Syntax error in SQL statement "
        '"CREATE TABLE t (id BIGINT PRIMARY KEY [*]GENERATED BY DEFAULT AS IDENTITY)"; expected "HASH"\n'
        "\tat org.h2.message.DbException.getJdbcSQLException(DbException.java:514)\n"
    )
    (tmp_path / "TEST-com.example.service.AppTests.xml").write_text(
        '<?xml version="1.0"?><testsuite name="com.example.service.AppTests"><testcase name="t" classname="com.example.service.AppTests">'
        f'<failure message="java.lang.IllegalStateException: Failed to load ApplicationContext" type="java.lang.IllegalStateException">{stack}</failure>'
        "</testcase></testsuite>",
        "utf-8",
    )
    cause = boot_cause(tmp_path)
    assert (
        cause.startswith("org.h2.jdbc.JdbcSQLSyntaxErrorException: Syntax error")
        and "[*]GENERATED" in cause
    )
    assert boot_cause(tmp_path / "없음") == ""


def test_public_tests_are_their_own_group(tmp_path: Path) -> None:
    _xml(tmp_path, "com.example.service.publictests.PersistenceCheckTest")
    _xml(
        tmp_path,
        "com.example.service.publictests.OrderPublicTest",
        ("org.opentest4j.AssertionFailedError", "x"),
    )
    got = {t.name: (t.group, t.result) for t in junit_results(tmp_path, {})}
    assert got == {
        "PersistenceCheckTest": ("public", "PASS"),
        "OrderPublicTest": ("public", "ASSERT"),
    }


def test_compile_errors_carry_symbol_and_location(tmp_path: Path) -> None:
    """2026-11-08: `Burst.IntUnaryOperator` — 재요청에 무엇이 없는지(symbol)와 어디서 찾았는지(location)를 함께 준다."""
    work = tmp_path / "w"
    work.mkdir()
    log = (
        f"{work.resolve()}/src/test/java/a/Hc1Test.java:51: error: cannot find symbol\n"
        "            Burst.IntUnaryOperator expectedStatus = i -> 200;\n"
        "                 ^\n"
        "  symbol:   class IntUnaryOperator\n"
        "  location: class Burst\n"
        f"{work.resolve()}/src/test/java/a/Hc2Test.java:7: error: ';' expected\n"
    )
    errs = compile_errors(log, work)
    assert [(e.path, e.line, e.detail) for e in errs] == [
        (
            "src/test/java/a/Hc1Test.java",
            51,
            "symbol: class IntUnaryOperator, location: class Burst",
        ),
        ("src/test/java/a/Hc2Test.java", 7, ""),
    ]
