"""생성 테스트의 건너뛰기 API — 숨은·공개 테스트가 쓰면 실행 전에 재요청한다 (#173).

건너뛴 테스트는 판정하지 않은 것이다. 결과 판정(verify·grade)은 건너뜀을 통과가 아니라 HARNESS_ERR 로 남기지만, 조건부 건너뛰기
("출력이 없으면 건너뜀")는 구현에 따라 판정이 갈려 숨은 테스트가 공정하지 않게 된다 — 쓰지 못하게 막는다.
건너뛰기 API 는 결과 형식(JUnit XML·vitest JSON·pytest XML)처럼 테스트 프레임워크 지식이라 스택 언어별로 여기 둔다.
템플릿 manifest `code_pitfalls` 는 시작 코드·참조 구현에만 돌고, 템플릿 vN 은 고치지 않는 규칙이라 새 판 없이 넣을 수 없었다.
"""

from __future__ import annotations

import re

from task_spec.schemas.template import TemplateManifest

# 스택 언어(manifest stack.language, 소문자) → 건너뛰기 API 정규식
SKIP_APIS: dict[str, tuple[str, ...]] = {
    "java": (  # JUnit 5
        r"@Disabled\b",
        r"\bassum(?:e|ing)(?:True|False|That)?\s*\(",  # Assumptions.assumeTrue·assumingThat
        r"\bAssumptions\s*\.",
        r"\bAbort\s*\.",
    ),
    "typescript": (  # Vitest
        r"\b(?:it|test|describe|suite)\s*\.\s*(?:skip|todo|skipIf|runIf)\b",
        r"\b(?:ctx|context|t)\s*\.\s*skip\s*\(",  # 조건부 ctx.skip()
    ),
    "python": (  # pytest·unittest
        r"\bpytest\s*\.\s*(?:skip|xfail|importorskip)\s*\(",
        r"@pytest\s*\.\s*mark\s*\.\s*(?:skip|skipif|xfail)\b",
        r"\bunittest\s*\.\s*(?:skip|skipIf|skipUnless|expectedFailure)\b",
        r"\.\s*skipTest\s*\(",
    ),
}

NOTE = (
    "건너뛴 테스트는 판정하지 않은 것이라 채점에서 통과로 세지 않는다 (HARNESS_ERR). 조건이 맞지 않으면 건너뛰지 말고 그 조건을 "
    "단언으로 확인해 실패시킨다"
)


def skip_hits(m: TemplateManifest, path: str, text: str) -> list[str]:
    """그 파일에서 건너뛰기 API 를 쓴 곳 — `경로:줄 \\`코드\\``. 모르는 스택은 빈 목록."""
    lang = m.stack.language.lower() if m.stack is not None else ""
    out: list[str] = []
    for pat in SKIP_APIS.get(lang, ()):
        for mt in re.finditer(pat, text):
            line = text.count("\n", 0, mt.start()) + 1
            out.append(f"{path}:{line} `{' '.join(mt.group(0).split())[:60]}`")
    return out
