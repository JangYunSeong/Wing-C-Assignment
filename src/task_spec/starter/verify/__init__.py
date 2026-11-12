"""템플릿 스택에 맞는 verifier — Java 는 Gradle, TypeScript(프론트엔드)는 Node, 데이터는 Python.

빌드(`build_and_repair`)의 verifier·compiler 로 쓴다. 2026-11-12 eval/starter 에서 옮김 (#126) — 알파(eval)와 builder(#129)가 같은 코드.
"""

from __future__ import annotations

from pathlib import Path

from task_spec.starter.templates import Template, is_java, is_python
from task_spec.starter.verify.gradle import GradleVerifier
from task_spec.starter.verify.node import NodeVerifier
from task_spec.starter.verify.python import PythonVerifier


def verifier_for(
    template: Template, work: Path, timeout: int | None = None, keep_code: bool = False
) -> GradleVerifier | NodeVerifier | PythonVerifier:
    """같은 인터페이스(compile_tests · compile_overlays · verify) — build_and_repair 의 verifier·compiler 둘 다로 쓴다."""
    if is_java(template.manifest):
        return GradleVerifier(template, work, timeout=timeout or 1200, keep_code=keep_code)
    if is_python(template.manifest):  # 데이터 템플릿 (2026-11-09)
        return PythonVerifier(template, work, timeout=timeout or 600, keep_code=keep_code)
    return NodeVerifier(template, work, timeout=timeout or 600, keep_code=keep_code)


__all__ = ["GradleVerifier", "NodeVerifier", "PythonVerifier", "verifier_for"]
