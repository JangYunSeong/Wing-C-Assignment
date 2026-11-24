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
from task_spec.starter.field_types import FieldType, parse
from task_spec.starter.seed_sql import time_kind

# 계약 형식 해석(field_types.parse)의 종류 → Python 타입 (#172: 자체 형식표가 enum 따옴표를 남기고 smallint·bigdecimal 을 몰랐다)
_PY = {
    "string": "str",
    "datetime": "str",
    "date": "str",
    "time": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict[str, Any]",
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
    """계약 필드 형식 → Python 타입 표기 (모르는 형식은 Any). 형식 해석은 field_types.parse 한 곳."""
    name = t.strip()
    # 엔티티 이름이 형식 이름(Record 등)과 겹쳐도 엔티티 — 이름 하나일 때만 (`Order[]` 는 목록)
    if re.fullmatch(r"\w+", name) and py_ident(name) in entities:
        return entities[py_ident(name)]
    return _py(parse(t), entities)


def _py(ft: FieldType, entities: dict[str, str]) -> str:
    if ft.kind == "list":
        return f"list[{_py(ft.items[0], entities) if ft.items else 'Any'}]"
    if ft.kind == "enum":
        out = "Literal[" + ", ".join(repr(v) for v in ft.values) + "]" if ft.values else "str"
    elif ft.kind == "ref":
        out = entities.get(py_ident(ft.name), "Any")
    else:
        out = _PY.get(ft.kind, "Any")
    return f"{out} | None" if ft.nullable and out != "Any" else out


def command_name(path: str) -> str:
    """명령 경로 → 함수 이름 (`pipeline/run.py` → `run_pipeline_run`)."""
    stem = re.sub(r"\.py$", "", path.strip("/"))
    return "run_" + py_ident(re.sub(r"[^A-Za-z0-9]+", "_", stem).lower())


def _option_param(f: ContractField) -> str:
    return f"{py_ident(f.name)}: str | int | float | os.PathLike[str]" + (
        "" if f.required else " | None = None"
    )


# 계약 형식 해석의 종류 → 하네스 write_parquet 열 형식 (string·int·float·bool·date·timestamp)
_PARQUET = {
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "date": "date",
    "datetime": "timestamp",
}
_SLOT = re.compile(r"\{(\w+)\}")
_PARTITION = re.compile(r"^(\w+)=")


_RESERVED = frozenset(
    {"ws", "rows", "name"}
)  # 도우미의 고정 인자 — 옵션 이름이 겹치면 도우미를 만들지 않는다


def _doc(text: str) -> str:
    """독스트링에 넣을 글 — 역슬래시·세 겹 따옴표를 이스케이프 (재료 요약·위치는 자유 글)."""
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _plural(snake: str) -> str:
    if snake.endswith("s"):
        return snake
    if snake.endswith("y") and not snake.endswith(("ay", "ey", "oy", "uy")):
        return snake[:-1] + "ies"
    return snake + "s"


def _snake(name: str) -> str:
    return py_ident(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())


def _file_helpers(contract: TaskContract) -> tuple[list[str], list[str]]:
    """재료 인터페이스가 역할·형식·행 엔티티를 정한 파일(Fixture.role — fit_interface)마다 입력 쓰기·출력 읽기 함수 (#256 2단계).
    위치 패턴의 `{옵션}` 마디는 함수 인자 — 날짜 옵션은 꼭 받고, 그 밖(폴더)은 작업 폴더 기준 상대 경로로 옵션 이름이 기본값.
    입력: 마지막 마디의 `*` 는 파일 이름 인자. 출력: `키=…` 마디가 있으면 파티션마다 {값: 행들}, 없으면 그 위치의 행 목록.
    역할이 없는 계약(인터페이스 없는 재료)은 아무것도 만들지 않는다 — 예전 contract.py 와 같다."""
    shapes = {m.name: m for m in contract.data_shapes()}
    opts: dict[str, str] = {}
    for e in contract.endpoints:
        if e.method:
            continue
        for f in [*e.request, *e.query]:
            opts[py_ident(f.name)] = time_kind(f) or "other"
    names: list[str] = []
    lines: list[str] = []
    used: set[str] = set()
    for fx in contract.fixtures:
        if not (fx.role and fx.format and fx.entity and fx.path) or fx.entity not in shapes:
            continue
        entity = shapes[fx.entity]
        typed = py_ident(entity.name)
        segs = fx.path.strip("/").split("/")
        slots = list(dict.fromkeys(py_ident(x) for x in _SLOT.findall(fx.path)))
        if any("*" in x for x in segs[:-1]) or _RESERVED & set(slots):
            continue  # 지원하지 않는 위치 모양 (가운데 마디의 *, 고정 인자와 같은 옵션 이름) — 도우미 없이 하네스로
        dated = [x for x in slots if opts.get(x) in ("date", "datetime")]
        dirs = [x for x in slots if x not in dated]
        params = [f"{x}: str" for x in dated] + [f'{x}: str = "{x}"' for x in dirs]

        def fmt(seg: str) -> str:
            return _SLOT.sub(lambda m: "{" + py_ident(m.group(1)) + "}", seg)

        if fx.role == "input":
            base = _plural(_snake(entity.name))
            name = f"write_{base}" if f"write_{base}" not in used else f"write_{fx.fixture_id}"
            last = segs[-1]
            default = last.replace("*", "batch-0001") if "*" in last else last
            rel = "/".join([*(fmt(x) for x in segs[:-1]), "{name}" if "*" in last else fmt(last)])
            if fx.format == "jsonl":
                call = f'ws.write_jsonl(f"{rel}", list(rows))'
            elif fx.format == "csv":
                cols = [f.name for f in entity.fields]
                call = f'ws.write_csv(f"{rel}", list(rows), columns={cols!r})'
            else:
                types = {f.name: _PARQUET.get(parse(f.type).kind, "string") for f in entity.fields}
                call = f'ws.write_parquet(f"{rel}", list(rows), {types!r})'
            sig = [*params, *([f'name: str = "{default}"'] if "*" in last else [])]
            kwonly = f", *, {', '.join(sig)}" if sig else ""
            lines += [
                "",
                "",
                f"def {name}(ws: Workspace, rows: Iterable[{typed}]{kwonly}) -> Path:",
                f'    """{fx.fixture_id} 입력 `{_doc(fx.path)}` — {_doc(fx.summary)}. 그 위치에 {fx.format} 파일 하나를 쓴다'
                + (" (같은 폴더에 하나 더 쓰려면 name 을 바꾼다)" if "*" in last else "")
                + ".",
                '    폴더 인자는 작업 폴더 기준 상대 경로 — 명령에는 ws.path(그 값) 을 넘긴다."""',
                f"    return {call}",
            ]
        else:
            part = next((i for i, x in enumerate(segs) if _PARTITION.match(x)), None)
            reader = {"parquet": "ws.read_parquet", "jsonl": "ws.read_jsonl", "csv": "ws.read_csv"}[
                fx.format
            ]
            if part is not None:
                key = _PARTITION.match(segs[part]).group(1)  # type: ignore[union-attr]
                literal = [x for x in segs[:part] if not _SLOT.search(x)]
                base = py_ident(literal[-1]) if literal else _plural(_snake(entity.name))
                name = f"read_{base}" if f"read_{base}" not in used else f"read_{fx.fixture_id}"
                root = "/".join(fmt(x) for x in segs[:part])
                body = [
                    f'    base = f"{root}"',
                    "    if not ws.exists(base):",
                    "        return {}",
                    f'    return {{v: {reader}(f"{{base}}/{key}={{v}}") for v in ws.partitions(base, "{key}")}}',
                ]
                ret = f"dict[str, list[{typed}]]"
                what = f"모든 {key} 파티션을 {{값: 행들}} 로 (읽는 쪽이 보는 것과 같게 — 이름이 _·. 로 시작하는 파일은 뺀다). 출력 폴더가 없으면 빈 dict"
            else:
                literal = [x for x in segs if not _SLOT.search(x) and "*" not in x]
                base = (
                    py_ident(literal[-1].split(".")[0]) if literal else _plural(_snake(entity.name))
                )
                name = f"read_{base}" if f"read_{base}" not in used else f"read_{fx.fixture_id}"
                where = (
                    "/".join(fmt(x) for x in segs[:-1])
                    if "*" in segs[-1]
                    else "/".join(fmt(x) for x in segs)
                )
                body = [f'    return {reader}(f"{where}")']
                ret = f"list[{typed}]"
                what = "그 위치의 행 목록 (없는 경로는 FileNotFoundError)"
            lines += [
                "",
                "",
                f"def {name}(ws: Workspace, *, {', '.join(params)}) -> {ret}:"
                if params
                else f"def {name}(ws: Workspace) -> {ret}:",
                f'    """{fx.fixture_id} 출력 `{_doc(fx.path)}` — {_doc(fx.summary)}. {what}. 깨진 파일은 예외 — 크래시 뒤에는 try 로 '
                '\'읽을 수 없음\'을 값으로 바꿔 단언한다."""',
                *body,
            ]
        used.add(name)
        names.append(name)
    return names, lines


def render_py_contract(
    contract: TaskContract, manifest: TemplateManifest, audience: Literal["hidden", "public"]
) -> GeneratedFile | None:
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    who = "숨은 케이스" if audience == "hidden" else "공개 테스트"
    entities = {py_ident(m.name): py_ident(m.name) for m in contract.data_shapes()}
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
    io_names, io_lines = _file_helpers(contract)
    if io_names:  # 입력 쓰기·출력 읽기 도우미 (#256 2단계) — 인터페이스가 정한 파일이 있을 때만 import 를 더한다
        lines[lines.index("import os")] = (
            "import os\nfrom collections.abc import Iterable\nfrom pathlib import Path"
        )
        lines[lines.index("from harness.grader import JobResult, run")] = (
            "from harness.grader import JobResult, Workspace, run"
        )
        lines[2] = (
            "명령은 run_…() 로, 입력 파일은 write_…() 로 쓰고 출력은 read_…() 로 읽는다 — 명령 경로·옵션 이름·파일 위치·필드 이름을 직접 쓰지 않는다."
        )
    names = (
        [py_ident(m.name) for m in contract.data_shapes()]
        + [command_name(e.path) for e in commands]
        + io_names
    )
    lines += [f"    {n!r}," for n in names] + ["]", ""]
    for m in contract.data_shapes():  # 테이블 엔티티 + 응답 모양
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
    lines += io_lines
    return GeneratedFile(path=rule.path, content="\n".join(lines) + "\n")
