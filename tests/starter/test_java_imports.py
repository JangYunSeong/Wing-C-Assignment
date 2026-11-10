"""Java 테스트의 빠진 JDK import 보정 (가상 코드)."""

from __future__ import annotations

from task_spec.schemas import GeneratedFile
from task_spec.starter.java_imports import ensure_java_imports

PATH = "src/test/java/com/example/service/grading/cases/RollbackTest.java"


def _fix(content: str) -> tuple[str, list[str]]:
    [f], notes = ensure_java_imports([GeneratedFile(path=PATH, content=content)])
    return f.content, notes


def test_missing_jdk_import_is_added_after_package() -> None:
    # 2026-11-10 v19-be3: 도우미 반환형 HttpResponse<String> 을 쓰고 import 를 빠뜨려 재요청 3번에도 컴파일 실패
    src = (
        "package com.example.service.grading.cases;\n\nimport java.util.List;\n\n"
        "class RollbackTest extends ContractCase {\n"
        "  void t() throws Exception { HttpResponse<String> r = get(1L); List<String> a = List.of(); }\n}\n"
    )
    out, notes = _fix(src)
    assert "import java.net.http.HttpResponse;" in out
    assert out.index("package") < out.index("import java.net.http.HttpResponse;")
    assert out.count("import java.util.List;") == 1 and notes == [
        f"{PATH}: JDK import HttpResponse 를 더함"
    ]


def test_names_in_strings_wildcards_and_own_classes_are_left_alone() -> None:
    src = (
        "package p;\nimport java.util.*;\nimport com.example.Duration;\n"
        'class T { // HttpResponse 주석\n  String s = "Instant"; Map<String, Duration> m; record Future(int x) {}\n'
        "  Future f; }\n"
    )
    out, notes = _fix(src)
    assert out == src and notes == []
