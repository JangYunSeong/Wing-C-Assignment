"""출력 스키마를 프롬프트에 넣을 짧은 글로 (프롬프트 머리말 schema_style: compact) [2026-11-06].

Pydantic 이 만든 JSON Schema 전체(title·$defs·중복 설명)는 길어서 규칙을 묻는다 — 필드마다 "이름: 타입 (제약) — 설명" 한 줄로 줄인다.
출력 검증은 그대로 Pydantic 모델이 한다 (형식 안전성은 같다). 기존 프롬프트(schema_style 없음)는 JSON Schema 그대로.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref:
        return defs[ref.rsplit("/", 1)[-1]]
    # Optional[X] → anyOf [X, null]
    any_of = [n for n in node.get("anyOf", []) if n.get("type") != "null"]
    if len(any_of) == 1:
        return {
            **_resolve(any_of[0], defs),
            **{k: v for k, v in node.items() if k not in ("anyOf",)},
        }
    return node


def _limits(node: dict[str, Any]) -> str:
    parts = []
    lo, hi = (
        node.get("minItems", node.get("minLength")),
        node.get("maxItems", node.get("maxLength")),
    )
    if node.get("type") == "array" and (lo is not None or hi is not None):
        parts.append(f"{lo}개 이상" if hi is None else f"{lo or 0}~{hi}개")
    elif node.get("type") == "string" and hi is not None:
        parts.append(f"≤{hi}자")
    lo_n, hi_n = node.get("minimum"), node.get("maximum")
    if lo_n is not None or hi_n is not None:
        parts.append(f"{lo_n if lo_n is not None else ''}~{hi_n if hi_n is not None else ''}")
    if node.get("pattern"):
        parts.append(f"형식 {node['pattern']}")
    return f" ({', '.join(parts)})" if parts else ""


def _type(node: dict[str, Any], defs: dict[str, Any], depth: int) -> str:
    node = _resolve(node, defs)
    if "enum" in node:
        return " | ".join(f'"{v}"' for v in node["enum"])
    if "const" in node:
        return f'"{node["const"]}"'
    t = node.get("type")
    if t == "array":
        item = _type(node.get("items", {}), defs, depth)
        return f"[{item}]"
    if t == "object" or "properties" in node:
        return _object(node, defs, depth + 1)
    return {"string": "문자열", "integer": "정수", "number": "수", "boolean": "참/거짓"}.get(
        t or "", "값"
    )


def _object(node: dict[str, Any], defs: dict[str, Any], depth: int) -> str:
    pad = "  " * depth
    required = set(node.get("required", []))
    lines = ["{"]
    for name, sub in node.get("properties", {}).items():
        full = _resolve(sub, defs)
        opt = "" if name in required else "?"
        desc = sub.get("description")  # 필드 설명만 — 클래스 설명(내부 메모)은 넣지 않는다
        lines.append(
            f"{pad}{name}{opt}: {_type(sub, defs, depth)}{_limits(full)}"
            + (f" — {desc}" if desc else "")
        )
    lines.append("  " * (depth - 1) + "}")
    return "\n".join(lines)


def compact_schema(model: type[BaseModel]) -> str:
    """JSON 객체 하나의 모양 — '?' 는 선택 필드."""
    schema = model.model_json_schema()
    return _object(schema, schema.get("$defs", {}), 1)
