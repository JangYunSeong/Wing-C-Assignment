"""시작할 때 실행하는 SQL 스크립트가 비지 않게 한다 (템플릿 manifest `init_scripts`).

2026-11-07 알파 테스트 (새 공고 kakaobank_2026#005): 시드가 0행이라 시작 코드 생성기가 템플릿 data.sql 의 `SELECT 1;` 을
주석만 남긴 채 덮어썼다. Spring 의 sql.init 은 주석을 걷어낸 스크립트가 비면 부팅에 실패해(`'script' must not be null or empty`)
시작 코드가 두 번 모두 boot_error → 재생성 예산을 다 쓰고 빌드 실패. 생성기에게 다시 묻지 않고 코드가 noop 문장을 붙인다 —
바꾼 것은 adjustments 로 남겨 승인 화면에 보인다.
"""

from __future__ import annotations

import re

from task_spec.schemas.build import GeneratedFile
from task_spec.schemas.template import TemplateManifest

_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE = re.compile(r"--[^\n]*")


def has_statement(sql: str) -> bool:
    """주석(`--`, `/* */`)과 공백·`;` 을 걷어내고 남는 것이 있는가."""
    body = _LINE.sub("", _BLOCK.sub("", sql))
    return bool(body.replace(";", "").strip())


def ensure_statements(
    files: list[GeneratedFile], manifest: TemplateManifest
) -> tuple[list[GeneratedFile], list[str]]:
    """init_scripts 경로의 파일이 실행할 문장 없이 쓰였으면 noop 문장을 붙인다. (바꾼 파일, 바꾼 내용 설명)."""
    rule = manifest.init_scripts
    if rule is None:
        return files, []
    out: list[GeneratedFile] = []
    notes: list[str] = []
    for f in files:
        if f.path in rule.paths and not has_statement(f.content):
            body = f.content.rstrip()
            f = GeneratedFile(path=f.path, content=(body + "\n" if body else "") + rule.noop + "\n")
            notes.append(
                f"{f.path}: 실행할 문장이 없어 `{rule.noop}` 을 붙임 (빈 스크립트면 앱이 부팅하지 못함)"
            )
        out.append(f)
    return out, notes
