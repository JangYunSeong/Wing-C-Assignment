"""계약 필드 타입 글 해석 — 앱 계약 타입(ts_helpers.ts_type)과 계약 적합성 스모크의 예시 값(conformance)이 같이 쓴다.

2026-11-11 재측정 토큰 갱신(db8bae00): 계약 필드 `proximity: (number|null)[]` 를 앱 타입은 배열(`unknown[]`)로, 스모크는 모르는 꼴로 읽어
글자("가상 proximity")를 응답에 넣었다 — 계약대로 `.map` 한 구현이 모두 TypeError, 게이트 ⓪ 이 시작 코드를 두 번 다시 만들고 예산을
다 썼다. 두 해석이 갈리지 않게 한 곳에서 읽는다.

계약 필드 타입은 생성기가 자유롭게 쓴다 (2026-11-10 저장 계약 집계: string·String·VARCHAR, long·bigint·Integer,
datetime·timestamp·LocalDateTime·DateTime …). 다루는 꼴: `X[]`·`(A|B)[]`·`array<X>`·`list<X>`·`enum(A|B)`·`A | null`·엔티티 이름·
object·map·json. 그 밖은 unknown (막지 않는다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCALARS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "datetime",
        (
            "datetime",
            "date-time",
            "timestamp",
            "localdatetime",
            "instant",
            "offsetdatetime",
            "zoneddatetime",
        ),
    ),
    ("date", ("date", "localdate")),
    ("time", ("time", "localtime")),
    ("integer", ("integer", "int", "long", "bigint", "short", "smallint", "int32", "int64")),
    ("number", ("number", "decimal", "bigdecimal", "double", "float", "numeric", "real")),
    ("boolean", ("boolean", "bool")),
    ("string", ("string", "str", "varchar", "text", "char", "uuid", "email", "url")),
    ("object", ("object", "map", "json", "record")),
)
_BY_NAME = {n: kind for kind, names in SCALARS for n in names}
_NULLS = frozenset(("null", "undefined", "none"))


@dataclass(frozen=True)
class FieldType:
    """kind: string·integer·number·boolean·datetime·date·time·object·enum·list·union·ref·unknown.
    list 는 items[0] 이 원소, union 은 items 가 null 을 뺀 선택지, enum 은 values, ref 는 name(엔티티 이름 또는 id)."""

    kind: str
    items: tuple[FieldType, ...] = ()
    values: tuple[str, ...] = ()
    name: str = ""
    nullable: bool = False


UNKNOWN = FieldType("unknown")


def _split_top(s: str, sep: str) -> list[str]:
    """괄호·꺾쇠 밖의 sep 로 나눈다."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [*out, cur]


def _unwrap(s: str) -> str:
    """바깥을 감싼 괄호 한 겹씩 — '(number|null)' → 'number|null'. '(a)|(b)' 처럼 앞뒤 괄호가 서로 짝이 아니면 그대로."""
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                return s
        s = s[1:-1].strip()
    return s


def parse(t: str) -> FieldType:
    """맨 바깥 | 를 먼저 나눈다 — TypeScript 처럼 `A | B[]` 는 A 또는 B 의 배열 (`(A|B)[]` 는 괄호가 묶는다).
    식별자는 ref 로 두고, 계약 엔티티에 있는지는 쓰는 쪽이 본다 (없으면 unknown — 앱 타입 unknown, 예시는 글자)."""
    s = _unwrap(t.strip())
    if not s:
        return UNKNOWN
    parts = [p.strip() for p in _split_top(s, "|")]
    if len(parts) > 1:
        nullable = any(p.lower() in _NULLS for p in parts)
        rest = [p for p in parts if p.lower() not in _NULLS]
        if rest and all(re.fullmatch(r"'[^']*'|\"[^\"]*\"", p) for p in rest):
            return FieldType("enum", values=tuple(p[1:-1] for p in rest), nullable=nullable)
        opts = tuple(parse(p) for p in rest)
        if len(opts) == 1:
            o = opts[0]
            return FieldType(o.kind, o.items, o.values, o.name, nullable=nullable)
        return FieldType("union", items=opts, nullable=nullable) if opts else UNKNOWN
    if s.endswith("[]"):
        return FieldType("list", items=(parse(s[:-2]),))
    m = re.fullmatch(r"(?:array|list)\s*<\s*(.+)\s*>", s, re.I)
    if m:
        return FieldType("list", items=(parse(m.group(1)),))
    m = re.fullmatch(r"enum\s*\((.*)\)", s, re.I)
    if m:
        vals = tuple(v.strip().strip("'\"") for v in re.split(r"[|,]", m.group(1)) if v.strip())
        return FieldType("enum", values=vals)
    if s.lower() in ("array", "list"):
        return FieldType("list", items=(UNKNOWN,))
    kind = _BY_NAME.get(s.lower())
    if kind is not None:
        return FieldType(kind)
    if re.fullmatch(r"[A-Za-z_$][\w$.]*", s):
        return FieldType("ref", name=s)
    return UNKNOWN


def non_null(ft: FieldType) -> FieldType:
    """null 을 뺀 첫 선택지 — 예시 값은 이 꼴로 만든다 (`A | null` 은 A 의 값)."""
    if ft.kind == "union" and ft.items:
        return ft.items[0]
    return ft
