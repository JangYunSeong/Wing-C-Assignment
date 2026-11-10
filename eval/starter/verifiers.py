"""템플릿 스택에 맞는 개발 서버 verifier — Java 는 Gradle, TypeScript(프론트엔드)는 Node (평가 전용). 2026-11-09 React 템플릿 연결."""

from __future__ import annotations

from pathlib import Path

from eval.starter.gradle_verify import GradleVerifier
from eval.starter.node_verify import NodeVerifier
from eval.starter.python_verify import PythonVerifier
from task_spec.starter.templates import Template, is_java, is_python


def verifier_for(
    template: Template, work: Path, timeout: int | None = None, keep_code: bool = False
) -> GradleVerifier | NodeVerifier | PythonVerifier:
    """같은 인터페이스(compile_tests · compile_overlays · verify) — build_and_repair 의 verifier·compiler 둘 다로 쓴다."""
    if is_java(template.manifest):
        return GradleVerifier(template, work, timeout=timeout or 1200, keep_code=keep_code)
    if is_python(template.manifest):  # 데이터 템플릿 (2026-11-09)
        return PythonVerifier(template, work, timeout=timeout or 600, keep_code=keep_code)
    return NodeVerifier(template, work, timeout=timeout or 600, keep_code=keep_code)
