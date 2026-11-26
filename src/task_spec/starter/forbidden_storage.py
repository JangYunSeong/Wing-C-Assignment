"""함정 틀이 금지한 저장 엔티티 대조 (#318 검사 3·4, 설계 검사 #332 T-F2·B-F7) — 한 규칙을 여러 검사가 같이 쓴다.

틀의 `forbidden_storage` 는 흔한 구현의 저장 방식(예: 세션을 DB 테이블에 둠)이라 지원자가 받는 공개 쪽 — 공개 계약(검사 4), 시작 코드
(정적 검사), README — 어디에도 테이블·엔티티로 나오면 안 된다. 예전에는 계약 검사만 엔티티 이름이 같거나 끝 s 하나 차이일 때 걸었고,
시작 코드 정적 검사는 이 목록을 받지 않았다 (발급 칸 예외가 세션 테이블을 오히려 통과시켰다 — 알파 r2 3/3 이 session·user_session 생성).

- 이름 대조(`names_entity`): 낱말 단위 — 이름의 끝 낱말들이 금지 엔티티의 낱말과 같으면 (단·복수 같게). `UserSession`·`user_session`·
  `LoginSession`·`sessions` 는 Session 이고, `SessionToken`·`OrderItem`(금지 Order) 은 아니다 (뒤에 붙은 낱말은 다른 엔티티일 수 있다).
- 글 대조(`storage_pattern`): 테이블 정의·조회 SQL·'<이름> 테이블'·엔티티 클래스·Repository·조회 메서드 꼴 — 명세 글(검사 3)과
  시작 코드·README 처럼 이름만 뽑을 수 없는 글에. `sessionId` 같은 공개 계약 칸은 걸리지 않는다.
- 틀 v3 는 엔티티를 영문 PascalCase 로 쓰지만 형식 제약은 없다 — 'Session 엔티티'처럼 써도 영문 식별자 부분으로 본다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from task_spec.schemas import ForbiddenStorage


def _ident(entity: str) -> str:
    """영문 식별자 부분 — 'Session 엔티티' → 'Session'."""
    return re.sub(r"[^0-9A-Za-z_]", "", entity)


def words(name: str) -> list[str]:
    """식별자를 소문자 낱말로 — 카멜·스네이크·약어 (HTTPSession → http, session)."""
    out: list[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", name):
        out += [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", part)]
    return out


def _plurals(word: str) -> set[str]:
    out = {f"{word}s", f"{word}es"}
    if word.endswith("y") and len(word) > 1:
        out.add(f"{word[:-1]}ies")
    return out


def _same_word(a: str, b: str) -> bool:
    return a == b or a in _plurals(b) or b in _plurals(a)


def _hangul_key(name: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", name.lower())


def names_entity(name: str, entity: str) -> bool:
    """name(계약 엔티티·테이블 이름)이 금지 엔티티를 가리키는가 — 끝 낱말들이 같으면 (앞에 낱말이 붙어도), 단·복수 같게.
    영문 식별자가 없는 금지 이름(예: '세션')은 정규화한 글자가 같을 때만."""
    want = words(entity)
    if not want:
        key = _hangul_key(entity)
        return bool(key) and _hangul_key(name) == key
    got = words(name)
    if len(got) < len(want):
        return False
    tail = got[-len(want) :]
    return tail[:-1] == want[:-1] and _same_word(tail[-1], want[-1])


def forbidden_for(name: str, forbidden: Sequence[ForbiddenStorage]) -> ForbiddenStorage | None:
    return next((s for s in forbidden if names_entity(name, s.entity)), None)


def storage_pattern(entity: str) -> re.Pattern[str] | None:
    """금지 엔티티를 저장 대상으로 그린 꼴 — 영문 식별자가 3자 이상일 때만. 단·복수·snake_case·앞에 낱말이 붙은 테이블 이름을 같게 본다."""
    name = re.sub(r"[^0-9A-Za-z]", "", _ident(entity))
    if len(name) < 3:
        return None
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    tables = "|".join(sorted({re.escape(name.lower()), re.escape(snake)}, key=len, reverse=True))
    table = rf"\w*(?:{tables})(?:e?s)?"
    ident = re.escape(name)
    return re.compile(
        rf"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"']?{table}\b"
        rf"|\b(?:from|join|update|into)\s+[`\"']?{table}\b"
        rf"|\b{table}\s*(?:테이블|table\b)"
        rf"|@Table\s*\(\s*name\s*=\s*[\"']{table}"
        rf"|\bclass\s+\w*{ident}\b"
        rf"|\b\w*{ident}Repository\b"
        rf"|\b(?:find|save|delete){ident}\w*\s*\(",
        re.IGNORECASE,
    )


def storage_hits(text: str, forbidden: Sequence[ForbiddenStorage]) -> list[tuple[str, str]]:
    """글에서 찾은 금지 저장 꼴 — (금지 엔티티, 찾은 글 40자)."""
    out: list[tuple[str, str]] = []
    for s in forbidden:
        pattern = storage_pattern(s.entity)
        if pattern is not None and (m := pattern.search(text)):
            out.append((s.entity, re.sub(r"\s+", " ", m.group(0))[:40]))
    return out
