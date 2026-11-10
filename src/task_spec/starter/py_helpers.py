"""Python 계약 도우미 — 데이터 템플릿(data/python-duckdb)의 숨은·공개 테스트가 import 하는 `contract.py` 를 계약에서 코드가 쓴다 (LLM 없음).

Java 의 ContractCase·TypeScript 의 contract.ts 와 같은 자리: 테스트가 명령 경로·옵션 이름·데이터 필드 이름을 손으로 쓰지 않게
명령 실행 함수(`run_<명령>`)와 데이터 모양(`TypedDict`)으로 준다. 없는 이름·옵션을 쓰면 수집(pytest --collect-only)·실행에서 드러난다.
CLI 계약: endpoints 의 method 없는 항목 = 명령 (path = 저장소 루트 기준 스크립트), request·query 필드 = 명령행 옵션 `--이름`(밑줄은 하이픈),
responses status = 종료 코드. 2026-11-09 데이터 템플릿 파이프라인 연결.
"""

from __future__ import annotations

import keyword
import re
from typing import Literal

from task_spec.schemas.build import ContractField, GeneratedFile, TaskContract
from task_spec.schemas.template import TemplateManifest

_SCALAR = {
    **dict.fromkeys(
        (
            "string",
            "text",
            "uuid",
            "date",
            "datetime",
            "date-time",
            "timestamp",
            "time",
            "email",
            "url",
        ),
        "str",
    ),
    **dict.fromkeys(("integer", "int", "long", "bigint", "number_int"), "int"),
    **dict.fromkeys(("number", "float", "double", "decimal"), "float"),
    **dict.fromkeys(("boolean", "bool"), "bool"),
    **dict.fromkeys(("array", "list"), "list[Any]"),
    **dict.fromkeys(("object", "map", "json", "dict"), "dict[str, Any]"),
}


def py_ident(name: str) -> str:
    out = re.sub(r"\W", "_", name).strip("_") or "x"
    if out[0].isdigit():
        out = f"x_{out}"
    return f"{out}_" if keyword.iskeyword(out) else out


def flag(name: str) -> str:
    """계약 필드 이름 → 명령행 옵션 (`raw_dir` → `--raw-dir`)."""
    return "--" + name.replace("_", "-")


def py_type(t: str, entities: dict[str, str]) -> str:
    """계약 필드 형식 → Python 타입 표기 (모르는 형식은 Any)."""
    t = t.strip()
    if t.endswith("[]"):
        return f"list[{py_type(t[:-2], entities)}]"
    m = re.fullmatch(r"(?:array|list)\s*<\s*(.+?)\s*>", t, re.I)
    if m:
        return f"list[{py_type(m.group(1), entities)}]"
    m = re.fullmatch(r"enum\s*\((.+)\)", t, re.I)
    if m:
        values = [v.strip() for v in re.split(r"[|,]", m.group(1)) if v.strip()]
        return "Literal[" + ", ".join(repr(v) for v in values) + "]" if values else "str"
    if py_ident(t) in entities:
        return entities[py_ident(t)]
    return _SCALAR.get(t.lower(), "Any")


def command_name(path: str) -> str:
    """명령 경로 → 함수 이름 (`pipeline/run.py` → `run_pipeline_run`)."""
    stem = re.sub(r"\.py$", "", path.strip("/"))
    return "run_" + py_ident(re.sub(r"[^A-Za-z0-9]+", "_", stem).lower())


def _option_param(f: ContractField) -> str:
    return f"{py_ident(f.name)}: str | int | float | os.PathLike[str]" + (
        "" if f.required else " | None = None"
    )


def render_py_contract(
    contract: TaskContract, manifest: TemplateManifest, audience: Literal["hidden", "public"]
) -> GeneratedFile | None:
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    who = "숨은 케이스" if audience == "hidden" else "공개 테스트"
    entities = {py_ident(m.name): py_ident(m.name) for m in contract.entities}
    lines = [
        f'"""이 과제의 공개 계약에서 빌드 파이프라인이 만든 도우미 — 손으로 고치지 않는다. {who}가 import 한다.',
        "",
        "명령은 run_…() 로, 입력 레코드는 아래 TypedDict 로 — 명령 경로·옵션 이름·필드 이름을 직접 쓰지 않는다.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import os",
        "from typing import Any, Literal, NotRequired, TypedDict",
        "",
        "from harness.grader import JobResult, run",
        "",
        "__all__ = [",
    ]
    commands = [e for e in contract.endpoints if not e.method]
    names = [py_ident(m.name) for m in contract.entities] + [command_name(e.path) for e in commands]
    lines += [f"    {n!r}," for n in names] + ["]", ""]
    for m in contract.entities:
        lines += [
            "",
            f"class {py_ident(m.name)}(TypedDict):",
            f'    """계약 데이터 모양 {m.entity_id} {m.name}"""',
            "",
        ]
        for f in m.fields:
            typ = py_type(f.type, entities)
            lines.append(f"    {py_ident(f.name)}: {typ if f.required else f'NotRequired[{typ}]'}")
    for e in commands:
        opts = [*e.request, *e.query]
        required = [f for f in opts if f.required]
        optional = [f for f in opts if not f.required]
        params = [_option_param(f) for f in [*required, *optional]]
        codes = ", ".join(f"{r.status} {r.description}" for r in e.responses)
        lines += [
            "",
            "",
            f"def {command_name(e.path)}(",
            "    *,",
            *[f"    {p}," for p in params],
            "    crash_after_bytes: int | None = None,",
            "    timeout: float | None = None,",
            ") -> JobResult:",
            f'    """{e.endpoint_id} `python {e.path}` — {e.summary} (종료 코드: {codes})"""',
            "    args: list[str] = []",
        ]
        for f in [*required, *optional]:
            v = py_ident(f.name)
            if f.required:
                lines.append(
                    f"    args += [{flag(f.name)!r}, os.fspath({v}) if isinstance({v}, os.PathLike) else str({v})]"
                )
            else:
                lines += [
                    f"    if {v} is not None:",
                    f"        args += [{flag(f.name)!r}, os.fspath({v}) if isinstance({v}, os.PathLike) else str({v})]",
                ]
        lines.append(
            f"    return run({e.path!r}, *args, crash_after_bytes=crash_after_bytes, timeout=timeout)"
        )
    return GeneratedFile(path=rule.path, content="\n".join(lines) + "\n")
