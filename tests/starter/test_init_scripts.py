"""시작 SQL 스크립트가 비지 않게 (2026-11-07 알파 테스트 — 시드 0행 → data.sql 주석뿐 → Spring 부팅 실패). 가상 데이터만."""

from __future__ import annotations

import pytest

from task_spec.schemas import GeneratedFile
from task_spec.starter.init_scripts import ensure_statements, has_statement
from task_spec.starter.templates import load_template
from tests.starter.test_contract import _manifest

JAVA = load_template("backend/java-spring-boot3-jpa")
DATA = "src/main/resources/data.sql"
SCHEMA = "src/main/resources/schema.sql"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("-- 시드 데이터\n-- AuditLog: 0행 (비어 있음)\n", False),
        ("/* 비어 있음 */\n  \n;\n", False),
        ("", False),
        ("SELECT 1;", True),
        ("-- 시드\nINSERT INTO orders (id) VALUES (1);\n", True),
        ("/* 여러 줄\n주석 */ INSERT INTO t VALUES (1);", True),
    ],
)
def test_has_statement(sql: str, expected: bool) -> None:
    assert has_statement(sql) is expected


def test_comment_only_script_gets_the_template_noop() -> None:
    files = [
        GeneratedFile(path=DATA, content="-- 시드 데이터\n-- AuditLog: 0행 (비어 있음)\n"),
        GeneratedFile(path=SCHEMA, content="CREATE TABLE audit_log (id BIGINT PRIMARY KEY);\n"),
        GeneratedFile(path="src/main/java/com/example/service/A.java", content="-- 아님"),
    ]
    out, notes = ensure_statements(files, JAVA.manifest)
    assert out[0].content == "-- 시드 데이터\n-- AuditLog: 0행 (비어 있음)\nSELECT 1;\n"
    assert out[1:] == files[1:]  # 문장이 있는 스크립트·스크립트 아닌 파일은 그대로
    assert len(notes) == 1 and DATA in notes[0] and "SELECT 1;" in notes[0]
    empty, notes = ensure_statements([GeneratedFile(path=DATA, content="  \n")], JAVA.manifest)
    assert empty[0].content == "SELECT 1;\n" and notes


def test_templates_without_the_rule_are_untouched() -> None:
    files = [GeneratedFile(path=DATA, content="-- 비어 있음\n")]
    assert ensure_statements(files, _manifest()) == (files, [])


def test_java_template_declares_its_init_scripts_and_ships_a_statement() -> None:
    rule = JAVA.manifest.init_scripts
    assert rule is not None and set(rule.paths) == {DATA, SCHEMA}
    for path in rule.paths:  # 템플릿 자신의 파일도 규칙을 지킨다
        assert has_statement((JAVA.project / path).read_text("utf-8")), path
