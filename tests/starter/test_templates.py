"""시작 코드 템플릿 레지스트리 (starter/templates.py) + 레포의 Java 템플릿 v1 — 가상 데이터만, 네트워크·빌드 없음.

Java 빌드·하네스 자체 점검(hidden/selftest)은 Gradle 이 필요해 단위 테스트에서 돌리지 않는다 (verifier·이미지 단계, #55).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.starter.contract import SpecView, check_contract, extract_values, spec_missing_params
from task_spec.starter.templates import (
    TEMPLATES_ROOT,
    available,
    candidate_layer_rules,
    check_template,
    current_version,
    forbidden_in,
    glob_match,
    load_template,
    slot_allows,
)

JAVA = "backend/java-spring-boot3-jpa"
CUR = f"v{current_version(JAVA)}"  # 복사한 템플릿에서 고칠 판 — load_template 이 CURRENT 를 읽는다


@pytest.fixture(scope="module")
def java():  # type: ignore[no-untyped-def]
    return load_template(JAVA)


def test_java_template_is_registered_and_clean(java) -> None:  # type: ignore[no-untyped-def]
    assert JAVA in available() and current_version(JAVA) == 7
    assert java.manifest.ref == f"{JAVA}@{CUR}"
    assert check_template(java) == []
    assert "build.gradle" in java.files("project")
    assert {"GraderSupport.java", "GraderJdbcHarness.java", "GraderParams.java"} <= set(
        java.files("hidden/harness")
    )


def test_java_v6_harness_settles_between_cases() -> None:
    """2026-11-26 교착 재료: 흔한 구현이 hc1 버스트에서 교착한 요청이 서버에 남아 다음 케이스 hc2 가 시간 초과(테스트 예외)였다.
    v6 하네스는 테스트 메서드가 끝날 때 서버(감싼 DataSource 의 쥐었거나 기다리는 연결)가 빌 때까지 기다리고, 비지 않으면 다음
    테스트를 새 앱에서 돌린다 — 모든 케이스·공개 테스트의 기반(GraderSupport)에 걸린다."""
    v6 = load_template(JAVA, 6)
    assert check_template(v6) == []
    harness = v6.root / "hidden/harness"
    support = (harness / "GraderSupport.java").read_text("utf-8")
    assert (
        "@TestExecutionListeners(listeners = GraderSettle.class, mergeMode = TestExecutionListeners.MergeMode.MERGE_WITH_DEFAULTS)"
        in support
    )
    settle = (harness / "GraderSettle.java").read_text("utf-8")
    assert "GraderJdbcHarness.settled(" in settle and "markApplicationContextDirty" in settle
    jdbc = (harness / "GraderJdbcHarness.java").read_text("utf-8")
    assert jdbc.count("inFlight.decrementAndGet()") == 2  # 얻지 못함 · 돌려줌
    assert "케이스 사이는 하네스가 정리한다" in (v6.root / "hidden/HARNESS.md").read_text("utf-8")
    # v5 는 그대로 (고정 칸 — hidden/ 은 같은 판에서 고치지 않는다)
    assert not (load_template(JAVA, 5).root / "hidden/harness/GraderSettle.java").exists()


def test_java_template_has_no_task_specific_code(java) -> None:  # type: ignore[no-untyped-def]
    """템플릿은 과제와 무관한 뼈대 — 손 예시(배송 감사)의 도메인 낱말이 남지 않는다."""
    text = "\n".join(
        (java.root / sub / f).read_text("utf-8", errors="ignore")
        for sub in ("project", "hidden/harness", "hidden/selftest")
        for f in java.files(sub)
        if not f.endswith(".jar")
    )
    for word in ("delivery", "Delivery", "hangyeol", "REQUIRES_NEW", "배송", "감사"):
        assert word not in text, word
    # 운영 값은 채점 때 하네스가 넣는다 — 뼈대 설정에는 박지 않는다 (spec-to-code §6)
    config = (java.project / "src/main/resources/application.yml").read_text("utf-8")
    for key in ("maximum-pool-size", "connection-timeout", "threads"):
        assert key not in config, key


def test_operating_config_keys_match_harness_code(java) -> None:  # type: ignore[no-untyped-def]
    """manifest harness_params[].config_keys == GraderSupport.OPERATING (운영 설정 덮어쓰기 표)."""
    code = (java.root / "hidden/harness/GraderSupport.java").read_text("utf-8")
    operating = dict(re.findall(r'm\.put\("(\w+)", "([^"]+)"\)', code))
    declared = {p.name: p.config_keys for p in java.manifest.harness_params if p.config_keys}
    assert operating == {name: keys[0] for name, keys in declared.items()}
    assert all(len(keys) == 1 for keys in declared.values())


def test_params_read_by_harness_are_declared(java) -> None:  # type: ignore[no-untyped-def]
    names = {p.name for p in java.manifest.harness_params}
    used: set[str] = set()
    for f in java.files("hidden/harness"):
        code = (java.root / "hidden/harness" / f).read_text("utf-8")
        used |= set(re.findall(r'GraderParams\.(?:long|int)Value\("(\w+)"', code))
    assert used and used <= names


# 프레임워크가 부르는 콜백(Spring BeanPostProcessor·TestExecutionListener)과 내부 도우미 — 케이스 작성자가 부르는 API 가 아니다
_HARNESS_INTERNAL = {
    "afterTestMethod",
    "getConnection",
    "postProcessAfterInitialization",
    "raw",
    "tag",
    "uri",
}


def test_harness_doc_covers_harness_api(java) -> None:  # type: ignore[no-untyped-def]
    """hidden/HARNESS.md (gen_hidden 입력) 가 하네스의 공개·protected 메서드를 모두 적는다.
    2026-11-07 스모크: 문서에 record·검사 예외가 빠져 생성된 숨은 테스트가 두 번 컴파일되지 않았다."""
    doc = (java.root / "hidden/HARNESS.md").read_text("utf-8")
    names: set[str] = set()
    records: set[str] = set()
    for f in java.files("hidden/harness"):
        code = (java.root / "hidden/harness" / f).read_text("utf-8")
        names |= set(re.findall(r"(?:public|protected) (?:static )?[\w<>, ?\[\]]+ (\w+)\(", code))
        records |= set(re.findall(r"public record (\w+)\(", code))
    missing = sorted(n for n in names - _HARNESS_INTERNAL - records if n not in doc)
    assert not missing, f"HARNESS.md 에 없는 하네스 메서드: {missing}"
    for r in records:
        assert re.search(rf"`[\w.]*{r}` 는 \*\*record\*\*", doc), r
    assert "throws Exception" in doc  # send·get·json 의 검사 예외


def test_java_spec_patterns_read_materials_like_the_hand_example(java) -> None:  # type: ignore[no-untyped-def]
    """손 예시 r2b 요청 자료 a2(운영 설정 발췌)·a3(요청 지표)·진행자 답과 같은 꼴의 가상 글에서 값을 뽑는다."""
    a2 = (
        "server:\n  tomcat:\n    threads:\n      max: 32\n"
        "spring:\n  datasource:\n    hikari:\n      maximum-pool-size: 8\n      connection-timeout: 5000\n"
    )
    a3 = "15:00 activeRequests=30\n15:01 activeRequests=32\n"
    answer = "마감 직전 동시 요청은 최대 32건입니다."
    m = java.manifest
    got = {p.name: extract_values(p, [a2, a3, answer]) for p in m.harness_params if p.visible}
    assert got == {
        "pool_size": ["8"],
        # 연결 대기 한도(connection_timeout_ms)는 v4 부터 숨긴 값 (빌드 보정) — 자료에서 읽지 않는다
        "server_threads": ["32"],
        "concurrency": ["32", "30"],
    }
    assert spec_missing_params([a2], m) == ["concurrency"]  # 피크 동시 요청은 지표·답에만


def test_java_failure_reasons_match_typical_logs(java) -> None:  # type: ignore[no-untyped-def]
    logs = {
        "pool_acquire_timeout": "HikariPool-1 - Connection is not available, request timed out after 5000ms "
        "(total=8, active=8, idle=0, waiting=24)",
        "rollback_left_success": "[reason:rollback_left_success] 배송 건 241",
        "request_timeout": "java.net.http.HttpTimeoutException: request timed out",
    }
    by_code = {r.code: r for r in java.manifest.failure_reasons}
    for code, line in logs.items():
        assert any(re.search(p, line) for p in by_code[code].log_patterns), code
    # 다른 사유 패턴에는 걸리지 않는다
    assert not any(
        re.search(p, logs["pool_acquire_timeout"])
        for p in by_code["rollback_left_success"].log_patterns
    )


@pytest.mark.parametrize(
    ("kind", "path", "ok"),
    [
        ("starter", "src/main/java/com/example/service/order/Order.java", True),
        ("starter", "src/main/resources/schema.sql", True),
        ("starter", "src/test/java/com/example/service/order/OrderApiTest.java", True),
        ("starter", "build.gradle", False),  # 잠김
        ("starter", "src/main/java/com/example/service/ServiceApplication.java", False),  # 잠김
        (
            "starter",
            "src/test/java/com/example/service/grading/cases/T1Test.java",
            False,
        ),  # 채점 자리
        ("starter", "src/test/resources/grader-params.properties", False),
        ("hidden", "src/test/java/com/example/service/grading/cases/T1Test.java", True),
        ("hidden", "src/test/resources/grader-params.properties", True),
        (
            "hidden",
            "src/test/java/com/example/service/grading/GraderSupport.java",
            False,
        ),  # 잠긴 하네스 자리
        ("hidden", "src/main/java/com/example/service/A.java", False),
        ("ref", "src/main/java/com/example/service/order/OrderService.java", True),
        ("ref", "src/test/java/com/example/service/order/OrderApiTest.java", False),
        ("public_test", "src/test/java/com/example/service/publictests/P1Test.java", True),
    ],
)
def test_slot_rules(java, kind: str, path: str, ok: bool) -> None:  # type: ignore[no-untyped-def]
    assert slot_allows(java.manifest, kind, path) is ok  # type: ignore[arg-type]


def test_candidate_image_must_not_hold_harness_or_manifest(java) -> None:  # type: ignore[no-untyped-def]
    """응시용 이미지 레이어 검사 [확정: 2026-11-07 사용자] — 템플릿 hidden/·manifest.yaml·채점 자리는 어느 층에도 없어야 한다."""
    rules = candidate_layer_rules(java)
    ok_tree = [f"workspace/{f}" for f in java.files("project")] + [
        "workspace/src/main/java/com/example/service/order/Order.java",
        "workspace/README.md",
    ]
    assert forbidden_in(ok_tree, rules) == []
    leaks = [
        "workspace/src/test/java/com/example/service/grading/GraderSupport.java",
        "workspace/src/test/java/com/example/service/grading/cases/T1Test.java",
        "workspace/src/test/resources/grader-params.properties",
        "opt/template/manifest.yaml",
        "opt/template/hidden/harness/Burst.java",
        "tmp/copy/GraderJdbcHarness.java",  # 다른 곳에 복사돼도 이름으로 잡는다
        "opt/template/README.tpl.md",
    ]
    assert forbidden_in(leaks, rules) == leaks


def test_check_template_catches_layer_rule_violations(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT / JAVA, root / JAVA)
    manifest = root / JAVA / CUR / "manifest.yaml"
    text = manifest.read_text("utf-8")
    manifest.write_text(
        text.replace("template_layer: [project/]", "template_layer: [project/, hidden/]").replace(
            "grader_only: [hidden/, manifest.yaml, README.tpl.md]", "grader_only: [README.tpl.md]"
        ),
        "utf-8",
    )
    problems = check_template(load_template(JAVA, root=root))
    assert any("template_layer" in p for p in problems)
    assert any("grader_only 에 hidden/" in p for p in problems)
    assert any("grader_only 에 manifest.yaml" in p for p in problems)


def test_check_template_catches_unknown_observable_param(tmp_path: Path) -> None:
    """#180: expect 에 써도 되는 조절값 선언은 실제 조절값 이름이어야 한다 — 오타면 아무것도 면제하지 못한 채 조용히 지나간다."""
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT / JAVA, root / JAVA)
    manifest = root / JAVA / CUR / "manifest.yaml"
    text = manifest.read_text("utf-8")
    assert "observable_params: [request_timeout_s, await_timeout_ms]" in text
    manifest.write_text(
        text.replace(
            "observable_params: [request_timeout_s, await_timeout_ms]",
            "observable_params: [request_timeout, await_timeout_ms]",
        ),
        "utf-8",
    )
    problems = check_template(load_template(JAVA, root=root))
    assert problems == [
        "hidden_rules.observable_params 의 request_timeout 가 harness_params 에 없음"
    ]


def test_check_template_catches_missing_harness_and_locked(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT / JAVA, root / JAVA)
    shutil.rmtree(root / JAVA / CUR / "hidden" / "selftest")
    (root / JAVA / CUR / "project" / "settings.gradle").unlink()
    problems = check_template(load_template(JAVA, root=root))
    assert any("selftest" in p for p in problems)
    assert any("settings.gradle" in p for p in problems)


def test_glob_semantics() -> None:
    assert glob_match("a/b/c/D.java", ["a/**/*.java"]) and glob_match("a/D.java", ["a/**/*.java"])
    assert not glob_match("a/b/D.kt", ["a/**/*.java"])
    assert glob_match("x/hidden/y/z.txt", ["**/hidden/**"]) and glob_match("hidden/z", ["hidden/"])
    assert not glob_match("a/bc", ["a/b"]) and not glob_match("ab/c", ["a/*"])


def test_contract_check_with_real_java_manifest(java) -> None:  # type: ignore[no-untyped-def]
    """2단계 manifest 로 1단계 계약 검사가 그대로 돈다 — 명세 자료에 운영 수치가 있으면 공백 없음."""
    from tests.starter.test_contract import _contract, _plan

    spec = SpecView(
        req_ids=("r1", "r2"),
        public_ac_ids=("p1", "p2"),
        hidden_acs={"h1": "t1"},
        trap_ids=("t1",),
        visible_texts=(
            "hikari:\n  maximum-pool-size: 8\n  connection-timeout: 5000\n",
            "activeRequests=32",
        ),
    )
    plan = _plan(
        harness={
            "pool_size": 8,
            "connection_timeout_ms": 5000,
            "concurrency": 32,
            "bursts": 3,
            "query_delay_ms": 20,
        }
    )
    res = check_contract(
        TaskContract.model_validate(_contract()),
        HiddenPlan.model_validate(plan),
        spec,
        java.manifest,
    )
    assert res.ok, (res.problems, res.spec_gaps)
    # 버스트 케이스 행 수 = concurrency × bursts (manifest burst_rows) — 값이 없으면 확인할 수 없어 재요청
    del plan["harness"]["bursts"]
    res = check_contract(
        TaskContract.model_validate(_contract()),
        HiddenPlan.model_validate(plan),
        spec,
        java.manifest,
    )
    assert any("버스트 행 수를 확인할 조절값" in p and "bursts" in p for p in res.problems)


def test_templates_dir_can_come_from_the_environment(tmp_path: Path) -> None:
    """패키지로 설치한 이미지(builder #129)에서는 소스 위치 기준 경로가 맞지 않아 TEMPLATES_DIR 로 준다."""
    import os
    import subprocess
    import sys

    code = "from task_spec.starter import templates as t; print(t.TEMPLATES_ROOT)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ | {"TEMPLATES_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(tmp_path)
